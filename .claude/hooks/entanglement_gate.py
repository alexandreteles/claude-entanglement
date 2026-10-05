#!/usr/bin/env python3
"""Claude Code integration for Entanglement.

Policy:
- File-level Maintainability Index with rating `red_low` is the only hard gate.
- Yellow/moderate and green/good MI pass.
- Function MI, cyclomatic complexity, cyclomatic density, and cognitive
  complexity are diagnostics only.

The hook scopes itself to files whose contents changed after the current Claude
Code session started. Pre-existing dirty files are ignored until this session
changes them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

METRICS = "mi,cc,density,cogc"
HARD_FAIL_RATING = "red_low"
MAX_DIAGNOSTIC_FILES = 8
MAX_FUNCTIONS_PER_FILE = 4
ANALYSIS_BUDGET_SECONDS = 20
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        # A malformed hook payload should not brick Claude Code.
        return 0

    if not isinstance(event, dict):
        return 0
    hook_event = event.get("hook_event_name")
    if hook_event not in {"SessionStart", "PostToolBatch", "PostToolUse", "Stop"}:
        return 0
    # Give Claude one continuation to refactor; never trap it in a Stop loop.
    if hook_event == "Stop" and event.get("stop_hook_active"):
        emit_json({"systemMessage": "Entanglement: repeated Stop check skipped to prevent a loop; this is not an MI pass."})
        return 0
    try:
        root = project_root(event)
        if root is None:
            return 0
        if hook_event == "SessionStart":
            remember_session_start(root, event)
            return 0
        if hook_event in {"PostToolBatch", "PostToolUse"}:
            if not batch_may_have_changed_files(event):
                return 0
            return post_tool_batch(root, event)
        return stop_gate(root, event)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"Entanglement could not evaluate this session: {exc}. Start a new session after fixing setup."
        if hook_event == "Stop":
            emit_json({"decision": "block", "reason": reason})
        elif hook_event in {"PostToolBatch", "PostToolUse"}:
            emit_json({"hookSpecificOutput": {"hookEventName": hook_event, "additionalContext": reason}})
        else:
            emit_json({"systemMessage": reason})
        return 0


def project_root(event: dict[str, Any]) -> Path | None:
    cwd = Path(
        event.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    result = run_git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="replace").strip()
    return Path(value).resolve() if value else None


def git_dir(root: Path) -> Path | None:
    result = run_git(root, ["rev-parse", "--git-dir"])
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="replace").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )


def nul_paths(data: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in data.split(b"\0")
        if item
    ]


def current_source_files(root: Path) -> list[str]:
    """Enumerate current Rust files, including committed and untracked files.

    Hashes, rather than differences from HEAD, define session changes. Ignored
    files, submodule contents, deletions, and symlinks are excluded.
    """
    result = run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    if result.returncode != 0:
        raise ValueError("Git could not enumerate source files")
    return sorted({rel for rel in nul_paths(result.stdout)
                   if Path(rel).suffix == ".rs" and safe_source(root, rel)})


def safe_source(root: Path, rel: str) -> bool:
    path = root / rel
    return (not path.is_symlink() and path.is_file()
            and path.resolve().is_relative_to(root))


def content_hash(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def session_state_path(root: Path, event: dict[str, Any]) -> Path | None:
    session_id = str(event.get("session_id") or "").strip()
    directory = git_dir(root)
    if not session_id or directory is None:
        return None
    safe_session = hashlib.sha256(session_id.encode()).hexdigest()
    return directory / "entanglement-claude" / f"{safe_session}.json"


def remember_session_start(root: Path, event: dict[str, Any]) -> None:
    state_path = session_state_path(root, event)
    if state_path is None:
        raise ValueError("missing session_id or Git directory")
    if state_path.exists():
        load_initial_files(root, event)  # Validate, but preserve the original baseline on resume.
        return
    initial: dict[str, str] = {}
    for rel in current_source_files(root):
        digest = content_hash(root / rel)
        if digest is None:
            raise ValueError(f"could not read {rel}")
        initial[rel] = digest
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({"version": 1, "initial_files": initial}, stream, sort_keys=True)
    try:
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


def load_initial_files(root: Path, event: dict[str, Any]) -> dict[str, str]:
    state_path = session_state_path(root, event)
    if state_path is None or not state_path.exists():
        raise ValueError("session baseline missing; restart Claude Code with SessionStart installed")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("invalid session baseline")
    value = payload.get("initial_files")
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in value.items()):
        raise ValueError("invalid session file hashes")
    return value


def session_changed_files(root: Path, event: dict[str, Any]) -> list[str]:
    initial = load_initial_files(root, event)
    relevant: list[str] = []
    for rel in current_source_files(root):
        current = content_hash(root / rel)
        if current is None:
            raise ValueError(f"could not read {rel}")
        if initial.get(rel) != current:
            relevant.append(rel)
    return relevant


def batch_may_have_changed_files(event: dict[str, Any]) -> bool:
    if event.get("hook_event_name") == "PostToolUse":
        return event.get("tool_name") not in READ_ONLY_TOOLS
    calls = event.get("tool_calls")
    if not isinstance(calls, list):
        return True
    for call in calls:
        if isinstance(call, dict) and call.get("tool_name") not in READ_ONLY_TOOLS:
            return True
    return False


def find_entanglement(root: Path) -> str | None:
    explicit = os.environ.get("ENTANGLEMENT_BIN")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(explicit) or explicit

    exe = "entanglement.exe" if os.name == "nt" else "entanglement"
    # Prefer the repository-local build over a potentially stale global install.
    for candidate in (
        root / "target" / "release" / exe,
        root / "target" / "debug" / exe,
    ):
        if candidate.is_file():
            return str(candidate)

    return shutil.which("entanglement")


def analyze_file(binary: str, root: Path, rel: str, timeout: float = ANALYSIS_BUDGET_SECONDS) -> tuple[dict[str, Any] | None, str | None]:
    path = root / rel
    if not safe_source(root, rel):
        return None, f"{rel}: source disappeared or is outside the repository"
    result = subprocess.run(
        [
            binary,
            "file",
            str(path),
            "--metrics",
            METRICS,
            "--format",
            "json",
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown analysis error"
        return None, f"{rel}: {error}"

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"{rel}: invalid Entanglement JSON ({exc})"

    if not isinstance(payload, dict):
        return None, f"{rel}: invalid Entanglement report object"
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None, f"{rel}: Entanglement returned no file analysis"
    report = files[0]
    value = mi(report) if isinstance(report, dict) else None
    score = value.get("score") if value else None
    if (not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(score) or not 0 <= score <= 100):
        return None, f"{rel}: missing or invalid file MI; rebuild Entanglement with MI support"
    expected = "red_low" if score < 10 else "yellow_moderate" if score < 20 else "green_good"
    if value.get("rating") != expected:
        return None, f"{rel}: missing or inconsistent MI rating"
    return report, None


def analyze_changed(
    root: Path, event: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], list[str], bool]:
    files = session_changed_files(root, event)
    if not files:
        return [], [], False

    binary = find_entanglement(root)
    if binary is None:
        # Entanglement currently supports Rust. Avoid blocking documentation or
        # configuration-only work when the analyzer itself is not available.
        source_files = [rel for rel in files if Path(rel).suffix.lower() == ".rs"]
        if source_files:
            return [], [
                "Entanglement could not be found. Build this repository or set "
                "ENTANGLEMENT_BIN before Claude Code starts."
            ], True
        return [], [], False

    reports: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []
    deadline = time.monotonic() + ANALYSIS_BUDGET_SECONDS
    for rel in files:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("Analysis budget exhausted; not all changed Rust files were evaluated")
            break
        try:
            report, error = analyze_file(binary, root, rel, timeout=remaining)
        except (OSError, subprocess.SubprocessError) as exc:
            report, error = None, f"{rel}: analyzer unavailable or timed out ({exc})"
        if error:
            errors.append(error)
        elif report is not None:
            reports.append((rel, report))
    return reports, errors, bool(files)


def mi(report: dict[str, Any]) -> dict[str, Any] | None:
    value = report.get("maintainability_index")
    return value if isinstance(value, dict) else None


def mi_score(value: dict[str, Any] | None) -> str:
    if not value:
        return "n/a"
    score = value.get("score")
    if isinstance(score, (int, float)):
        return f"{score:.2f}"
    return "n/a"


def function_sort_key(function: dict[str, Any]) -> tuple[float, float, float]:
    cognitive = function.get("cognitive_complexity")
    cc = function.get("cyclomatic_complexity")
    density = function.get("cyclomatic_density")
    return (
        float(cognitive) if isinstance(cognitive, (int, float)) else 0.0,
        float(cc) if isinstance(cc, (int, float)) else 0.0,
        float(density) if isinstance(density, (int, float)) else 0.0,
    )


def diagnostics(reports: list[tuple[str, dict[str, Any]]]) -> str:
    if not reports:
        return ""

    lines = [
        "Entanglement diagnostics for files changed in this Claude Code session.",
        "Only file-level red/low Maintainability Index is enforced; all function metrics below are informational.",
    ]

    for rel, report in reports[:MAX_DIAGNOSTIC_FILES]:
        file_mi = mi(report)
        rating = file_mi.get("rating", "n/a") if file_mi else "n/a"
        nloc = report.get("nloc", "n/a")
        lines.append(f"- {rel}: MI {mi_score(file_mi)} ({rating}), NLOC {nloc}")

        functions = report.get("functions")
        if not isinstance(functions, list):
            continue
        ranked = sorted(
            (fn for fn in functions if isinstance(fn, dict)),
            key=function_sort_key,
            reverse=True,
        )[:MAX_FUNCTIONS_PER_FILE]
        for fn in ranked:
            fn_mi = mi(fn)
            name = fn.get("name", "<anonymous>")
            cc = fn.get("cyclomatic_complexity", "n/a")
            density = fn.get("cyclomatic_density", "n/a")
            cogc = fn.get("cognitive_complexity", "n/a")
            if isinstance(density, (int, float)):
                density = f"{density:.3f}"
            lines.append(
                f"  - {name}: MI {mi_score(fn_mi)}, CC {cc}, density {density}, CogC {cogc}"
            )

    if len(reports) > MAX_DIAGNOSTIC_FILES:
        lines.append(f"- … {len(reports) - MAX_DIAGNOSTIC_FILES} additional changed files omitted")
    return "\n".join(lines)


def red_low_reports(
    reports: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    failures = []
    for rel, report in reports:
        value = mi(report)
        if value and value.get("rating") == HARD_FAIL_RATING:
            failures.append((rel, report, value))
    return failures


def emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")


def post_tool_batch(root: Path, event: dict[str, Any]) -> int:
    reports, errors, had_changed_files = analyze_changed(root, event)
    if not had_changed_files:
        return 0

    parts: list[str] = []
    report_text = diagnostics(reports)
    if report_text:
        parts.append(report_text)
    if errors:
        parts.append("Entanglement analysis warnings:\n- " + "\n- ".join(errors))
    if not parts:
        return 0

    emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": event.get("hook_event_name", "PostToolBatch"),
                "additionalContext": "\n\n".join(parts),
            }
        }
    )
    return 0


def stop_gate(root: Path, event: dict[str, Any]) -> int:
    reports, errors, had_changed_files = analyze_changed(root, event)
    if not had_changed_files:
        return 0

    failures = red_low_reports(reports)
    if not failures and not errors:
        return 0

    parts: list[str] = []
    if failures:
        lines = [
            "Entanglement MI gate failed. These files changed in this session have red/low Maintainability Index (MI < 10):"
        ]
        for rel, _report, value in failures:
            lines.append(
                f"- {rel}: MI {mi_score(value)}; volume {value.get('volume', 'n/a')}; "
                f"CC {value.get('cyclomatic_complexity', 'n/a')}; NLOC {value.get('nloc', 'n/a')}"
            )
        lines.extend(
            [
                "Refactor the failing files until their file-level MI leaves the red/low band.",
                "Preserve behavior and public interfaces unless the task itself requires changing them.",
                "Use the other Entanglement metrics as diagnostic evidence, not as hard thresholds.",
            ]
        )
        parts.append("\n".join(lines))

    if errors:
        parts.append(
            "The MI gate could not evaluate all changed source files:\n- "
            + "\n- ".join(errors)
            + "\nResolve the analysis errors before completing the task."
        )

    report_text = diagnostics(reports)
    if report_text:
        parts.append(report_text)

    emit_json({"decision": "block", "reason": "\n\n".join(parts)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
