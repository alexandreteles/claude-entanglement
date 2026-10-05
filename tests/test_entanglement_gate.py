from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "entanglement_gate.py"


class HookIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "lib.rs").write_text("pub fn clean() {}\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "initial")

        self.fake = self.repo / ("fake-entanglement.py")
        self.fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import pathlib
                import sys

                path = pathlib.Path(sys.argv[2])
                source = path.read_text(encoding='utf-8')
                if 'RED_MI' in source:
                    score, rating = 7.5, 'red_low'
                elif 'YELLOW_MI' in source:
                    score, rating = 15.0, 'yellow_moderate'
                else:
                    score, rating = 42.0, 'green_good'

                print(json.dumps({
                    'files': [{
                        'path': str(path),
                        'nloc': 20,
                        'maintainability_index': {
                            'score': score,
                            'rating': rating,
                            'volume': 100.0,
                            'cyclomatic_complexity': 9,
                            'nloc': 20,
                        },
                        'functions': [{
                            'name': 'work',
                            'maintainability_index': {
                                'score': 30.0,
                                'rating': 'green_good',
                                'volume': 50.0,
                                'cyclomatic_complexity': 99,
                                'nloc': 10,
                            },
                            'cyclomatic_complexity': 99,
                            'cyclomatic_density': 9.9,
                            'cognitive_complexity': 123,
                        }],
                    }]
                }))
                """
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            self.fake.chmod(0o755)
        self.session = "test-session"
        self.call_hook("SessionStart")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def call_hook(self, event_name: str, tool_calls=None, **extra):
        payload = {
            "session_id": self.session,
            "cwd": str(self.repo),
            "hook_event_name": event_name,
        }
        if event_name == "PostToolBatch":
            payload["tool_calls"] = tool_calls or [
                {"tool_name": "Edit", "tool_input": {}, "tool_response": "ok"}
            ]
        payload.update(extra)
        env = os.environ.copy()
        env["ENTANGLEMENT_BIN"] = str(self.fake)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.repo,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_red_file_blocks_stop(self):
        (self.repo / "src" / "lib.rs").write_text("// RED_MI\npub fn work() {}\n", encoding="utf-8")
        output = self.call_hook("Stop")
        self.assertEqual(output["decision"], "block")
        self.assertIn("red/low", output["reason"])
        self.assertIn("src/lib.rs", output["reason"])

    def test_yellow_file_passes_even_with_extreme_complexity_metrics(self):
        (self.repo / "src" / "lib.rs").write_text("// YELLOW_MI\npub fn work() {}\n", encoding="utf-8")
        output = self.call_hook("Stop")
        self.assertIsNone(output)

    def test_post_tool_batch_returns_informational_metrics(self):
        (self.repo / "src" / "lib.rs").write_text("pub fn changed() {}\n", encoding="utf-8")
        output = self.call_hook("PostToolBatch")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("informational", context)
        self.assertIn("CC 99", context)
        self.assertIn("CogC 123", context)

    def test_preexisting_dirty_file_is_ignored_until_changed_in_session(self):
        # New session begins with an already-dirty file.
        (self.repo / "src" / "lib.rs").write_text("// RED_MI\npub fn dirty() {}\n", encoding="utf-8")
        self.session = "dirty-session"
        self.call_hook("SessionStart")
        self.assertIsNone(self.call_hook("Stop"))

        (self.repo / "src" / "lib.rs").write_text("// RED_MI\npub fn touched() {}\n", encoding="utf-8")
        output = self.call_hook("Stop")
        self.assertEqual(output["decision"], "block")

    def test_read_only_batch_does_not_emit_diagnostics(self):
        (self.repo / "src" / "lib.rs").write_text("pub fn changed() {}\n", encoding="utf-8")
        output = self.call_hook(
            "PostToolBatch",
            tool_calls=[{"tool_name": "Read", "tool_input": {}, "tool_response": "..."}],
        )
        self.assertIsNone(output)

    def test_post_tool_use_fallback_emits_matching_event(self):
        (self.repo / "src/lib.rs").write_text("pub fn work() {}\n")
        output = self.call_hook("PostToolUse", tool_name="Edit")
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("CC 99", output["hookSpecificOutput"]["additionalContext"])

    def test_committed_session_change_is_still_checked(self):
        (self.repo / "src/lib.rs").write_text("// RED_MI\npub fn work() {}\n")
        self.git("add", "src/lib.rs")
        self.git("commit", "-m", "session change")
        self.assertEqual(self.call_hook("Stop")["decision"], "block")

    def test_resume_preserves_baseline(self):
        (self.repo / "src/lib.rs").write_text("// RED_MI\npub fn work() {}\n")
        self.call_hook("SessionStart", source="resume")
        self.assertEqual(self.call_hook("Stop")["decision"], "block")

    def test_stop_loop_escape(self):
        (self.repo / "src/lib.rs").write_text("// RED_MI\npub fn work() {}\n")
        output = self.call_hook("Stop", stop_hook_active=True)
        self.assertNotIn("decision", output)
        self.assertIn("not an MI pass", output["systemMessage"])

    def test_new_untracked_rust_file_is_checked(self):
        (self.repo / "new file.rs").write_text("// RED_MI\npub fn work() {}\n")
        self.assertIn("new file.rs", self.call_hook("Stop")["reason"])

    def test_reverted_session_change_passes(self):
        (self.repo / "src/lib.rs").write_text("// RED_MI\npub fn work() {}\n")
        self.git("restore", "src/lib.rs")
        self.assertIsNone(self.call_hook("Stop"))

    def test_documentation_only_change_passes_without_analyzer(self):
        self.fake.unlink()
        (self.repo / "README.md").write_text("Documentation\n")
        self.assertIsNone(self.call_hook("Stop"))

    def test_missing_baseline_reports_setup_error(self):
        self.session = "not-started"
        self.assertIn("baseline missing", self.call_hook("Stop")["reason"])

    def test_corrupted_baseline_is_reported(self):
        state = next((self.repo / ".git/entanglement-claude").glob("*.json"))
        state.write_text("[]")
        self.assertIn("invalid session baseline", self.call_hook("Stop")["reason"])

    def test_missing_mi_fails_instead_of_silently_passing(self):
        self.fake.write_text("#!/usr/bin/env python3\nprint('{\"files\": [{}]}')\n")
        (self.repo / "src/lib.rs").write_text("pub fn work() {}\n")
        self.assertIn("invalid file MI", self.call_hook("Stop")["reason"])

    def test_missing_executable_produces_actionable_error(self):
        self.fake.unlink()
        (self.repo / "src/lib.rs").write_text("pub fn work() {}\n")
        self.assertIn("analyzer unavailable", self.call_hook("Stop")["reason"])

    def test_mcp_batch_injects_diagnostics(self):
        (self.repo / "src/lib.rs").write_text("pub fn work() {}\n")
        output = self.call_hook("PostToolBatch", tool_calls=[{"tool_name": "mcp__fs__write_file"}])
        self.assertIn("additionalContext", output["hookSpecificOutput"])

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_external_symlink_is_excluded(self):
        (self.repo / "linked.rs").symlink_to(self.fake)
        self.assertIsNone(self.call_hook("Stop"))

    def test_linked_worktree_uses_its_git_directory(self):
        worktree = self.repo / "linked-worktree"
        self.git("worktree", "add", "-b", "hook-test", str(worktree))
        self.repo = worktree
        self.session = "worktree-session"
        self.call_hook("SessionStart")
        (self.repo / "src/lib.rs").write_text("// RED_MI\npub fn work() {}\n")
        self.assertEqual(self.call_hook("Stop")["decision"], "block")


if __name__ == "__main__":
    unittest.main()
