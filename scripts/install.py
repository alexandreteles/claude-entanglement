#!/usr/bin/env python3
"""Install project hooks while preserving unrelated Claude Code settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import os

SOURCE = Path(__file__).resolve().parents[1]


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(project: Path, shared: bool = False) -> Path:
    project = project.resolve()
    result = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=True)
    if Path(result.stdout.strip()).resolve() != project:
        raise ValueError("pass the Git repository root")
    settings = project / ".claude" / ("settings.json" if shared else "settings.local.json")
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Claude settings must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.hooks must be an object")
    template = json.loads((SOURCE / ".claude" / "settings.json").read_text())["hooks"]
    for event, entries in template.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"hooks.{event} must be an array")
        handler = entries[0]["hooks"][0]
        handler["command"] = sys.executable  # No shell; supports spaces in the interpreter path.
        duplicate = any(isinstance(group, dict) and any(
            isinstance(item, dict) and item.get("args") == handler["args"]
            for item in group.get("hooks", [])) for group in existing)
        if not duplicate:
            existing.append({"hooks": [handler]})
    destination = project / ".claude" / "hooks" / "entanglement_gate.py"
    source_data = (SOURCE / ".claude" / "hooks" / "entanglement_gate.py").read_bytes()
    # Parse and validate settings before making changes. Preserve the first backup.
    for path in (settings, destination):
        backup = path.with_name(path.name + ".entanglement.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
    atomic_write(destination, source_data)
    atomic_write(settings, (json.dumps(data, indent=2) + "\n").encode())
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Git project root")
    parser.add_argument("--shared", action="store_true", help="use settings.json instead of settings.local.json")
    args = parser.parse_args()
    try:
        settings = install(args.project, args.shared)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Install failed: {exc}\n")
    print(f"Installed Entanglement hooks in {settings}. Start a new Claude Code session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
