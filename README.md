# claude-entanglement

A Claude Code hook that uses [Entanglement](https://github.com/alexandreteles/entanglement) to report Rust code metrics and request refactoring when a changed file has low maintainability.

Only **file-level Maintainability Index (MI) below 10**, reported as `red_low`, triggers the quality gate. Yellow MI (10–<20) and green MI (20–100) pass. Function MI, cyclomatic complexity (CC), cyclomatic density, and cognitive complexity (CogC) are diagnostic information only.

## Requirements

- Python 3.10 or newer and Git, available to the Claude Code process.
- A current Claude Code release with `PostToolBatch` and command-hook `args` support. Run `claude update` and inspect `/hooks` after installation. For older releases, use the `PostToolUse` configuration described below.
- Entanglement built from a revision with `--metrics` and file-level MI. Verified against commit [`da8ec494a0620ae3eaea3495efa0686693ef0444`](https://github.com/alexandreteles/entanglement/commit/da8ec494a0620ae3eaea3495efa0686693ef0444). Version `0.1.0` alone does not identify a compatible build.

The hook uses Python's standard library. Rust and a C toolchain are needed to build Entanglement, not to run this hook with an existing analyzer binary.

## Setup

### 1. Build Entanglement

Clone the analyzer separately from the project you want Claude to edit:

```sh
git clone https://github.com/alexandreteles/entanglement.git
cd entanglement
git checkout da8ec494a0620ae3eaea3495efa0686693ef0444
cargo fetch --locked
cargo build --release --locked
cd ..
```

`cargo fetch` also downloads platform dependencies used by Entanglement's offline build-time grammar discovery. This revision uses Rust edition 2024; use a current stable toolchain. Validation used Rust 1.99.0.

### 2. Install the hook in your Rust project

```sh
git clone https://github.com/alexandreteles/claude-entanglement.git
python3 claude-entanglement/scripts/install.py /absolute/path/to/your-rust-project
```

Pass the **Git repository root**, including for a linked worktree. The installer copies `.claude/hooks/entanglement_gate.py` and merges three events into `.claude/settings.local.json`. It preserves other settings and hooks, records the Python executable used for installation, and creates `.entanglement.bak` backups of existing files. Running it again does not duplicate its entries. Local settings stay on your machine.

For a configuration you intend to commit, add `--shared` to write `.claude/settings.json` instead. The generated interpreter path is machine-specific: change each handler's `command` to `python3`, `python`, or your team's agreed Python executable before sharing. Use one installation mode per project. If these hooks already exist in another settings scope, remove that copy first to avoid duplicate execution.

### 3. Select the analyzer and start Claude

On macOS/Linux:

```sh
export ENTANGLEMENT_BIN=/absolute/path/to/entanglement/target/release/entanglement
cd /absolute/path/to/your-rust-project
claude
```

On Windows, run the installer with `python` (Python 3.10+), then use PowerShell:

```powershell
$env:ENTANGLEMENT_BIN = 'C:\path\to\entanglement\target\release\entanglement.exe'
Set-Location 'C:\path\to\your-rust-project'
claude
```

Set the variable in the environment that launches Claude, including when using an IDE or Desktop. An explicitly configured analyzer that fails is reported; the hook does not silently substitute a different build.

Without `ENTANGLEMENT_BIN`, the hook tries the target project's `target/release/entanglement[.exe]`, then `target/debug/entanglement[.exe]`, then `entanglement` on `PATH`.

Start a **new session** after installing. Confirm `SessionStart`, `PostToolBatch`, and `Stop` appear in `/hooks`. Trust the project when Claude prompts you.

### Manual installation / older Claude Code releases

Copy `.claude/hooks/entanglement_gate.py` into the target project and merge the `hooks` entries from this repository's `.claude/settings.json` into your settings. Do not replace existing settings. The sample uses `python3`; adjust it to your Python executable.

The supplied settings use direct execution with `command` and `args`, so paths with spaces do not need shell quoting. If your Claude Code release does not support those fields or `PostToolBatch`, update it. Alternatively, replace the `PostToolBatch` entry with `PostToolUse`, matcher `Write|Edit|Bash|PowerShell|NotebookEdit`, and a shell-form command such as `python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/entanglement_gate.py"` (omit `args`). Apply the same shell-form change to the other handlers. The script accepts `PostToolUse` and emits the matching event name. This fallback runs after successful calls only; Stop still checks all changed Rust files.

## What happens during a session

1. `SessionStart` snapshots hashes of tracked and non-ignored untracked `.rs` files. State lives under the worktree's Git directory in `entanglement-claude/<hashed-session-id>.json`. Resuming a session preserves its original snapshot.
2. `PostToolBatch` checks the current contents against that snapshot and injects concise file and function diagnostics. Known read-only batches are skipped; unknown tools, including MCP tools, are checked.
3. `Stop` requests refactoring if any changed file reports file-level `red_low`. Missing binaries, missing MI fields, invalid reports, and analyzer failures also block the first Stop with setup feedback.

The analyzer command is:

```sh
entanglement file /absolute/path/to/file.rs --metrics mi,cc,density,cogc --format json
```

The hook reads source files and runs the analyzer; it does not edit or roll back code. Source changes committed during the session remain eligible for checking. Files already dirty at session start are ignored until their contents change. Files restored to their session-start contents pass out of scope.

### Stop-loop protection

When Claude receives a blocking Stop response, it gets a continuation to act on the feedback. If the next Stop arrives with `stop_hook_active: true`, the hook allows the turn to finish and displays a message explaining that the check was skipped. **This is a bounded feedback loop, not an unconditional completion guarantee.** A skipped check does not certify that MI passed. Use a separate CI quality check if you need mandatory enforcement.

## Scope and troubleshooting

- The current analyzer supports Rust. Other file types, deleted files, symlinks, submodule contents, and ignored untracked files are excluded. Work outside a Git repository is skipped.
- Scope is based on content changes, not who wrote them. Changes by another process in the same worktree can be included. Use separate worktrees for concurrent sessions.
- Per-file analysis provides local diagnostic context. Cross-file recursion information may differ from `entanglement repo` analysis.
- A baseline is required. Missing, corrupt, or prototype-format state produces setup feedback; start a new session. With Claude closed, obsolete state can be removed from the worktree's Git directory.
- If `--metrics` is rejected or MI is absent, rebuild the pinned Entanglement revision. The workspace's original binary was stale even though the checked-out source supported these features.
- Analyzer calls share a 20-second budget per hook invocation. Timeouts report incomplete analysis. Settings allow 60 seconds for diagnostics/Stop and 30 seconds for SessionStart; very large repositories may require tuning both the hook budget and event timeout.
- The integration was tested on Linux through real stdin/stdout hook payloads. A live interactive Claude Code session and Windows execution were not available for validation.

To uninstall, remove this tool's handlers from your Claude settings and delete `.claude/hooks/entanglement_gate.py`. Leave unrelated handlers in place. Backups can restore the original files if you have made no subsequent changes.

## Tests

From this repository:

```sh
python3 -m unittest discover -s tests -v
```

The fake-analyzer tests cover MI policy, commits, untracked files, dirty baselines, resumes, loop protection, MCP batches, worktrees, missing MI, and setup failures. Installer tests verify settings preservation, idempotence, and paths with spaces. A timeout test checks that incomplete analysis produces feedback.

To include the real Entanglement integration test:

```sh
ENTANGLEMENT_TEST_BIN=/absolute/path/to/entanglement/target/release/entanglement \
  python3 -m unittest discover -s tests -v
```

All 23 tests passed against the pinned analyzer revision. The real-analyzer test checks green/red file MI, function CC, hook output, and unchanged source contents.

## Documentation checked

The configuration and event payloads were checked against Anthropic's [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) on October 5, 2026, including command-hook exec form, `PostToolBatch`, and Stop decision control. The prototype's `PostToolBatch` and `args` fields are supported by the current documentation.
