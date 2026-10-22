"""Unit and fixture-based tests for build_trace analyzer."""

import json
import unittest

from build_trace import (
    BuildAnalysisParser,
    CriticalPathAnalyzer,
    LogParseError,
    format_text_report,
)


class TestBuildTraceAnalyzer(unittest.TestCase):

    def test_malformed_and_incomplete_logs(self):
        """Test parser behavior on malformed JSON and incomplete records."""
        malformed_jsonl = (
            '{"run_id":"r1","reason":"build-started","jobs":4}\n'
            'NOT_VALID_JSON_LINE\n'
            '{"run_id":"r1","reason":"unit-registered","index":0,"package_id":"pkg#0.1.0"}\n'
            '{"run_id":"r1"}\n'  # Missing reason
        )

        # In non-strict mode, it records malformed lines without throwing
        parser = BuildAnalysisParser(strict=False)
        parser.parse_str(malformed_jsonl)
        self.assertEqual(len(parser.malformed_lines), 2)
        self.assertIn(0, parser.units)

        # In strict mode, it raises LogParseError
        parser_strict = BuildAnalysisParser(strict=True)
        with self.assertRaises(LogParseError) as cm:
            parser_strict.parse_str(malformed_jsonl)
        self.assertEqual(cm.exception.line_no, 2)

    def test_missing_events_tolerance(self):
        """Test handling of units that start but never finish, or missing registrations."""
        log = (
            '{"reason":"build-started","jobs":2}\n'
            '{"reason":"unit-graph-finished","elapsed":0.05}\n'
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.1}\n'
            # Unit 0 never emits finished!
            '{"reason":"unit-started","index":1,"elapsed":0.3}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.8}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        crit_path = analyzer.compute_critical_path()

        # Should complete analysis without throwing
        self.assertEqual(crit_path.terminal_unit_index, 1)
        self.assertEqual(len(crit_path.segments), 1)
        self.assertTrue(len(crit_path.unreconstructed_notes) > 0)

    def test_fresh_units_handling(self):
        """Test that fresh units have no execution timestamps and unblocking by fresh units is detected."""
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.02}\n'
            # Unit 0 is fresh (cached)
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"fresh"}\n'
            # Unit 1 depends on Unit 0 and is dirty
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale":"src/main.rs"}}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.10}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.40}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)

        self.assertEqual(parser.units[0].status, "fresh")
        self.assertIsNone(parser.units[0].start_elapsed)
        self.assertIsNone(parser.units[0].finish_elapsed)

        analyzer = CriticalPathAnalyzer(parser)
        crit_path = analyzer.compute_critical_path()

        self.assertEqual(len(crit_path.segments), 1)
        seg = crit_path.segments[0]
        self.assertEqual(seg.unit_index, 1)
        self.assertTrue(seg.is_observed_root)
        self.assertIn("fresh dependencies", seg.note)

    def test_metadata_pipelining(self):
        """Test critical path calculation under metadata pipelining where downstream starts before upstream finishes."""
        # Timeline:
        # t=0.00: graph finished
        # t=0.10: Unit 0 (lib) starts
        # t=0.40: Unit 0 produces rmeta, unblocking Unit 1
        # t=0.50: Unit 1 (app) starts (scheduler gap = 0.50 - 0.40 = 0.10s)
        # t=0.70: Unit 0 fully finishes codegen
        # t=0.90: Unit 1 finishes
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"lib#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-rmeta-finished","index":0,"elapsed":0.40,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.50}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.70}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.90}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        crit_path = analyzer.compute_critical_path()

        self.assertEqual(len(crit_path.segments), 2)
        seg0 = crit_path.segments[0]
        seg1 = crit_path.segments[1]

        # Unit 0 was only required up to rmeta
        self.assertEqual(seg0.unit_index, 0)
        self.assertEqual(seg0.phase_unblocked, "rmeta")
        self.assertAlmostEqual(seg0.start_elapsed, 0.10)
        self.assertAlmostEqual(seg0.effective_end_elapsed, 0.40)
        self.assertAlmostEqual(seg0.execution_duration, 0.30)
        self.assertAlmostEqual(seg0.scheduler_gap_before, 0.10)  # 0.10 - 0.00

        # Unit 1 started after being unblocked by rmeta
        self.assertEqual(seg1.unit_index, 1)
        self.assertEqual(seg1.unblocked_by_index, 0)
        self.assertEqual(seg1.unblocked_by_kind, "rmeta")
        self.assertAlmostEqual(seg1.scheduler_gap_before, 0.10)  # 0.50 - 0.40
        self.assertAlmostEqual(seg1.execution_duration, 0.40)    # 0.90 - 0.50

        # Total execution on critical path = 0.30 (lib rmeta) + 0.40 (app finish) = 0.70
        self.assertAlmostEqual(crit_path.total_execution_duration, 0.70)
        # Total scheduler gaps = 0.10 (initial) + 0.10 (between rmeta and app start) = 0.20
        self.assertAlmostEqual(crit_path.total_scheduler_gaps, 0.20)
        # Total duration = 0.90 - 0.10 + 0.10 = 0.90
        self.assertAlmostEqual(crit_path.total_observed_path_duration, 0.90)

    def test_explicit_dirty_reason_chain(self):
        """Test recursive tracing of explicit dependency dirty reasons."""
        log = (
            '{"reason":"build-started"}\n'
            # Unit 0 modified directly
            '{"reason":"unit-registered","index":0,"package_id":"leaf#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"dirty","cause":{"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale":"leaf/src/lib.rs"}}\n'
            # Unit 1 rebuilt because unit 0 changed
            '{"reason":"unit-registered","index":1,"package_id":"mid#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"unit-dependency-info-changed","unit":0}}\n'
            # Unit 2 rebuilt because unit 1 changed
            '{"reason":"unit-registered","index":2,"package_id":"top#0.1.0","dependencies":[1]}\n'
            '{"reason":"unit-fingerprint","index":2,"status":"dirty","cause":{"dirty_reason":"unit-dependency-info-changed","unit":1}}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()

        self.assertIn(2, chains)
        chain2 = chains[2]
        self.assertEqual(chain2.full_chain, [2, 1, 0])
        self.assertEqual(chain2.root_cause_index, 0)
        self.assertIn("file modified: leaf/src/lib.rs", chain2.root_cause_summary)
        self.assertFalse(chain2.is_ambiguous)

    def test_ambiguous_dirty_reasons_distinction(self):
        """Test that dirty units with generic causes and rebuilt dependencies are marked ambiguous."""
        log = (
            '{"reason":"build-started"}\n'
            # Unit 0 is dirty
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"dirty","cause":{"dirty_reason":"features-changed","old":"","new":"foo"}}\n'
            # Unit 1 is dirty with non-specific cause 'nothing-obvious'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"nothing-obvious"}}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()

        self.assertIn(1, chains)
        chain1 = chains[1]
        self.assertTrue(chain1.is_ambiguous)
        self.assertIn("nothing-obvious", chain1.ambiguity_reason)

    def test_full_text_report_output(self):
        """Test formatting of complete text report."""
        log = (
            '{"reason":"build-started","run_id":"test-run-123","command":["cargo","build"],"jobs":2,"host":"x86_64-linux"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.01}\n'
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0","target":{"name":"core","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"new"}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.02}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.15}\n'
        )
        parser = BuildAnalysisParser()
        parser.parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()
        crit_path = analyzer.compute_critical_path()
        report = format_text_report(parser, analyzer, chains, crit_path)

        self.assertIn("CARGO INCREMENTAL BUILD CRITICAL-PATH ANALYSIS REPORT", report)
        self.assertIn("test-run-123", report)
        self.assertIn("OBSERVED EXECUTION CRITICAL PATH", report)
        self.assertIn("TIMING AND CRITICAL-PATH BUDGET", report)


if __name__ == "__main__":
    unittest.main()
