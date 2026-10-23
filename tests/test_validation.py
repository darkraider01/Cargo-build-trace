"""Validation must fail on missing or mismatched artifacts, including no-op builds."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_trace import BuildAnalysisParser
from scripts.run_validation_scenarios import compare_timings, run_cmd


class TestValidationArtifacts(unittest.TestCase):
    def test_saved_sessions_match_only_their_own_timing_reports(self):
        root = Path(__file__).resolve().parent.parent / "validation_output"
        for summary in json.loads((root / "all_scenarios_summary.json").read_text()):
            name = summary["scenario"]
            with self.subTest(scenario=name):
                parser = BuildAnalysisParser(strict=True).parse_file(root / name / f"{name}_session.jsonl")
                records = compare_timings(parser, root / name / f"{name}_cargo_timing.html", summary["executed_units"])
                self.assertEqual(len(records), summary["executed_units"])
                self.assertEqual(summary["timing_agreement_pct"], 100.0 if records else None)
                self.assertEqual(parser.session.run_id, summary["run_id"])

    def test_missing_or_incomplete_timing_artifacts_fail(self):
        parser = BuildAnalysisParser()
        with tempfile.TemporaryDirectory() as root:
            html = Path(root) / "timing.html"
            with self.assertRaises(FileNotFoundError):
                compare_timings(parser, html, 0)
            html.write_text("<html>No timing data</html>")
            with self.assertRaisesRegex(ValueError, "UNIT_DATA"):
                compare_timings(parser, html, 0)
            html.write_text('const UNIT_DATA = [];')
            self.assertEqual(compare_timings(parser, html, 0), [])
            with self.assertRaisesRegex(AssertionError, "expected 1"):
                compare_timings(parser, html, 1)

    def test_occupied_output_directory_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            sentinel = Path(root) / "all_scenarios_summary.json"
            sentinel.write_text("previous results")
            script = Path(__file__).resolve().parent.parent / "scripts/run_validation_scenarios.py"
            result = subprocess.run([sys.executable, str(script), "--output-dir", root], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not empty", result.stderr)
            self.assertEqual(sentinel.read_text(), "previous results")

    def test_command_failure_stops_validation_and_uses_private_cargo_home(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root) / "workspace"
            ws.mkdir()
            with patch("scripts.run_validation_scenarios.subprocess.run") as execute, patch("sys.stderr"):
                execute.return_value = subprocess.CompletedProcess(["cargo"], 1, "", "intentional failure")
                with self.assertRaises(RuntimeError):
                    run_cmd(["cargo", "build"], ws)
                self.assertEqual(execute.call_args.kwargs["env"]["CARGO_HOME"], str(Path(root) / "cargo_home"))
                self.assertEqual(execute.call_args.kwargs["env"]["CARGO_TARGET_DIR"], str(ws / "target"))
