from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("gate", ROOT / ".claude/hooks/entanglement_gate.py")
installer = load("installer", ROOT / "scripts/install.py")


class SetupTests(unittest.TestCase):
    def test_install_preserves_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="project with spaces ") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
            settings = repo / ".claude/settings.local.json"
            settings.parent.mkdir()
            original = {"permissions": {"allow": ["Read"]}, "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}}
            settings.write_text(json.dumps(original))
            installer.install(repo)
            installer.install(repo)
            data = json.loads(settings.read_text())
            self.assertEqual(data["permissions"], original["permissions"])
            self.assertEqual(len(data["hooks"]["Stop"]), 2)
            self.assertEqual(data["hooks"]["Stop"][1]["hooks"][0]["command"], sys.executable)
            self.assertEqual(json.loads(settings.with_name(settings.name + ".entanglement.bak").read_text()), original)
            self.assertTrue((repo / ".claude/hooks/entanglement_gate.py").exists())

    def test_invalid_settings_are_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
            settings = repo / ".claude/settings.local.json"
            settings.parent.mkdir()
            settings.write_text('{"hooks": {"Stop": {}}}')
            before = settings.read_bytes()
            with self.assertRaises(ValueError):
                installer.install(repo)
            self.assertEqual(settings.read_bytes(), before)
            self.assertFalse((repo / ".claude/hooks/entanglement_gate.py").exists())

    def test_analyzer_timeout_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "file.rs").write_text("fn test() {}")
            with patch.object(hook, "session_changed_files", return_value=["file.rs"]), \
                 patch.object(hook, "find_entanglement", return_value="entanglement"), \
                 patch.object(hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("entanglement", 20)):
                reports, errors, changed = hook.analyze_changed(repo, {})
            self.assertTrue(changed)
            self.assertFalse(reports)
            self.assertIn("timed out", errors[0])


@unittest.skipUnless(os.environ.get("ENTANGLEMENT_TEST_BIN"), "set ENTANGLEMENT_TEST_BIN for real analyzer checks")
class RealAnalyzerTests(unittest.TestCase):
    def test_current_analyzer_metrics_and_stop_contract(self):
        binary = str(Path(os.environ["ENTANGLEMENT_TEST_BIN"]).resolve())
        with tempfile.TemporaryDirectory(prefix="real analyzer ") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            source = repo / "sample.rs"
            source.write_text("fn initial() {}\n")
            environment = dict(os.environ, ENTANGLEMENT_BIN=binary)
            script = ROOT / ".claude/hooks/entanglement_gate.py"

            def call(event, **extra):
                result = subprocess.run([sys.executable, str(script)], cwd=repo, env=environment,
                    input=json.dumps(dict(session_id="real-test", cwd=str(repo), hook_event_name=event, **extra)),
                    text=True, capture_output=True, check=True)
                self.assertFalse(result.stderr)
                return json.loads(result.stdout) if result.stdout.strip() else None

            call("SessionStart")
            source.write_text("fn changed(x: bool) -> i32 { if x { 1 } else { 0 } }\n")
            report, error = hook.analyze_file(binary, repo, "sample.rs")
            self.assertIsNone(error)
            self.assertEqual(report["maintainability_index"]["rating"], "green_good")
            self.assertEqual(report["functions"][0]["cyclomatic_complexity"], 2)
            self.assertIsNone(call("Stop"))
            output = call("PostToolBatch", tool_calls=[{"tool_name": "Edit"}])
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolBatch")
            self.assertIn("CC 2", output["hookSpecificOutput"]["additionalContext"])
            source.write_text("\n".join(f"fn function_{i}(x: bool) -> i32 {{ if x {{ {i} }} else {{ 0 }} }}" for i in range(450)))
            before = source.read_bytes()
            report, error = hook.analyze_file(binary, repo, "sample.rs")
            self.assertIsNone(error)
            self.assertEqual(report["maintainability_index"]["rating"], "red_low")
            self.assertEqual(call("Stop")["decision"], "block")
            self.assertEqual(source.read_bytes(), before)
