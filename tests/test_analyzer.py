"""Independent unit and synthetic graph tests for build_trace analyzer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_trace import (
    BuildAnalysisParser,
    CriticalPathAnalyzer,
    LogParseError,
    format_text_report,
    serialize_analysis,
)


class TestBuildTraceAnalyzer(unittest.TestCase):

    def test_simple_dependency_chain(self):
        """Test a clean linear dependency chain A -> B."""
        # Timeline:
        # t=0.00: graph finished
        # t=0.10: Unit 0 starts
        # t=0.30: Unit 0 finishes, unblocks Unit 1
        # t=0.35: Unit 1 starts (ready delay = 0.05)
        # t=0.60: Unit 1 finishes
        log = (
            '{"reason":"build-started","jobs":2}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"requested":true}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.30,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.35}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.60}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        self.assertEqual(res.target_unit_index, 1)
        self.assertEqual(len(res.segments), 2)

        seg0, seg1 = res.segments[0], res.segments[1]
        self.assertEqual(seg0.unit_index, 0)
        self.assertEqual(seg0.phase_unblocked, "finish")
        self.assertAlmostEqual(seg0.execution_duration, 0.20)
        self.assertAlmostEqual(seg0.ready_to_start_delay, 0.10)

        self.assertEqual(seg1.unit_index, 1)
        self.assertEqual(seg1.unblocked_by_index, 0)
        self.assertEqual(seg1.unblocked_by_kind, "finish")
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.05)
        self.assertAlmostEqual(seg1.execution_duration, 0.25)

        self.assertAlmostEqual(res.total_execution_duration, 0.45)
        self.assertAlmostEqual(res.total_ready_to_start_delays, 0.15)
        self.assertAlmostEqual(res.total_chain_duration, 0.60)

    def test_two_parallel_branches(self):
        """Test two independent parallel branches joining at a final binary."""
        # Branch 1: Unit 0 -> Unit 1
        # Branch 2: Unit 2
        # Target: Unit 3 depends on [1, 2].
        # Unit 2 finishes late at 0.50 and is the unblocker of Unit 3.
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"a#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"b#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-registered","index":2,"package_id":"c#0.1.0"}\n'
            '{"reason":"unit-registered","index":3,"package_id":"target#0.1.0","dependencies":[1, 2],"requested":true}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-started","index":2,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.25}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.40}\n'
            '{"reason":"unit-finished","index":2,"elapsed":0.50,"unblocked":[3]}\n'
            '{"reason":"unit-started","index":3,"elapsed":0.52}\n'
            '{"reason":"unit-finished","index":3,"elapsed":0.70}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        # The observed unblocking chain should follow Unit 2 (the final unblocker) -> Unit 3
        self.assertEqual(res.target_unit_index, 3)
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.segments[0].unit_index, 2)
        self.assertEqual(res.segments[1].unit_index, 3)
        self.assertEqual(res.segments[1].unblocked_by_index, 2)

    def test_multiple_dependencies_joining_unblocker(self):
        """Verify that when multiple dependencies finish, only the final unblocker is recorded."""
        # Unit 2 depends on 0 and 1.
        # 0 finishes at 0.20, does not unblock 2.
        # 1 finishes at 0.30, unblocks 2.
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"d0#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"d1#0.1.0"}\n'
            '{"reason":"unit-registered","index":2,"package_id":"target#0.1.0","dependencies":[0, 1]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.30,"unblocked":[2]}\n'
            '{"reason":"unit-started","index":2,"elapsed":0.32}\n'
            '{"reason":"unit-finished","index":2,"elapsed":0.50}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path(target_unit_index=2)

        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.segments[0].unit_index, 1)
        self.assertEqual(res.segments[1].unit_index, 2)
        self.assertEqual(res.segments[1].unblocked_by_index, 1)

    def test_metadata_pipelining(self):
        """Test critical path calculation under metadata pipelining where downstream starts before upstream finishes."""
        # Timeline:
        # t=0.00: graph finished
        # t=0.10: Unit 0 (lib) starts
        # t=0.40: Unit 0 produces rmeta, unblocking Unit 1
        # t=0.50: Unit 1 (lib) starts (ready delay = 0.50 - 0.40 = 0.10s)
        # t=0.70: Unit 0 fully finishes codegen
        # t=0.90: Unit 1 finishes
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"lib#0.1.0","target":{"name":"lib","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"target":{"name":"app","kind":"lib"}}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-rmeta-finished","index":0,"elapsed":0.40,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.50}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.70}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.90}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        self.assertEqual(len(res.segments), 2)
        seg0, seg1 = res.segments[0], res.segments[1]

        # Unit 0 contributed up to rmeta
        self.assertEqual(seg0.unit_index, 0)
        self.assertEqual(seg0.phase_unblocked, "rmeta")
        self.assertAlmostEqual(seg0.start_elapsed, 0.10)
        self.assertAlmostEqual(seg0.effective_end_elapsed, 0.40)
        self.assertAlmostEqual(seg0.execution_duration, 0.30)
        self.assertAlmostEqual(seg0.ready_to_start_delay, 0.10)

        # Unit 1 unblocked by rmeta
        self.assertEqual(seg1.unit_index, 1)
        self.assertEqual(seg1.unblocked_by_index, 0)
        self.assertEqual(seg1.unblocked_by_kind, "rmeta")
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.10)
        self.assertAlmostEqual(seg1.execution_duration, 0.40)

        self.assertAlmostEqual(res.total_execution_duration, 0.70)
        self.assertAlmostEqual(res.total_ready_to_start_delays, 0.20)
        self.assertAlmostEqual(res.total_chain_duration, 0.90)

    def test_full_artifact_dependency(self):
        """Test that linkable targets (bin) require full completion of dependencies."""
        # Unit 0 is lib, Unit 1 is bin. Unit 1 cannot start on rmeta alone.
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"lib#0.1.0","target":{"name":"lib","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"cli#0.1.0","dependencies":[0],"target":{"name":"cli","kind":"bin"}}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-rmeta-finished","index":0,"elapsed":0.30}\n'  # does NOT unblock cli!
            '{"reason":"unit-finished","index":0,"elapsed":0.50,"unblocked":[1]}\n'  # unblocks cli on full finish
            '{"reason":"unit-started","index":1,"elapsed":0.55}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.80}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        seg0 = res.segments[0]
        self.assertEqual(seg0.phase_unblocked, "finish")
        self.assertAlmostEqual(seg0.effective_end_elapsed, 0.50)
        self.assertAlmostEqual(seg0.execution_duration, 0.40)

    def test_synthetic_scheduling_edge_regression(self):
        """Regression test for cli_app, orchestrator, and string_parser case.

        cli_app depends directly on orchestrator.
        orchestrator depends on string_parser.
        string_parser unblocks cli_app because of synthetic transitive linking edges.
        """
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":5,"package_id":"string_parser#0.1.0","target":{"name":"sp","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":4,"package_id":"orchestrator#0.1.0","dependencies":[5],"target":{"name":"orch","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":0,"package_id":"cli_app#0.1.0","dependencies":[4],"target":{"name":"cli","kind":"bin"}}\n'
            '{"reason":"unit-started","index":5,"elapsed":0.05}\n'
            '{"reason":"unit-rmeta-finished","index":5,"elapsed":0.10,"unblocked":[4]}\n'
            '{"reason":"unit-started","index":4,"elapsed":0.12}\n'
            '{"reason":"unit-finished","index":4,"elapsed":0.15}\n'
            '{"reason":"unit-finished","index":5,"elapsed":0.18,"unblocked":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.19}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.30}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)

        # 1. Check registered vs effective dependencies
        cli = parser.units[0]
        self.assertEqual(cli.dependencies, [4])
        # Effective dependencies must include direct 4 (all) and synthetic 5 (all)
        eff_indices = {ed.dep_index: ed for ed in cli.effective_dependencies}
        self.assertIn(4, eff_indices)
        self.assertFalse(eff_indices[4].is_synthetic)
        self.assertIn(5, eff_indices)
        self.assertTrue(eff_indices[5].is_synthetic)

        # 2. Check unblocking chain reconstruction
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path(target_unit_index=0)

        # Target 0 was unblocked by 5 (synthetic edge), which in turn was unblocked at root
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.segments[0].unit_index, 5)
        self.assertEqual(res.segments[1].unit_index, 0)
        self.assertEqual(res.segments[1].unblocked_by_index, 5)
        self.assertFalse(res.segments[1].is_registered_dep_of_successor)

    def test_ready_unit_delayed_by_job_limit(self):
        """Test detection and attribution of large ready-to-start delay under job limits."""
        # Unit 0 finishes at 0.20, unblocking Unit 1.
        # But job slot is busy with Unit 2 from 0.20 to 0.70.
        # Unit 1 starts at 0.72. Ready delay = 0.52.
        log = (
            '{"reason":"build-started","jobs":1}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.72}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.90}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        seg1 = res.segments[1]
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.52)
        self.assertAlmostEqual(seg1.execution_duration, 0.18)

    def test_fresh_dependencies_handling(self):
        """Test that units with only fresh dependencies are handled without failing."""
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.02}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"fresh"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale_item":"changed-file","stale":"src/main.rs"}}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.10}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.40}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        self.assertEqual(len(res.segments), 1)
        seg = res.segments[0]
        self.assertTrue(seg.is_observed_root)
        self.assertIn("fresh dependencies", seg.note)
        self.assertTrue(len(res.unreconstructed_notes) > 0)

    def test_missing_unblocking_events(self):
        """Test diagnostic reporting when a unit starts without an unblock event despite having dependencies."""
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.02}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.10}\n'  # did not list 1 in unblocked!
            '{"reason":"unit-started","index":1,"elapsed":0.15}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.30}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        # Step should be root with missing unblocking event note
        seg = res.segments[0]
        self.assertTrue(seg.is_observed_root)
        self.assertIn("Missing unblocking event", seg.note)

    def test_multiple_requested_targets_selection(self):
        """Test that explicit target flag overrides default requested target selection."""
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"app1#0.1.0","requested":true}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app2#0.1.0","requested":true}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.35}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)

        # Default chooses the latest requested target (index 1)
        res_default = analyzer.compute_critical_path()
        self.assertEqual(res_default.target_unit_index, 1)

        # Explicit target chooses index 0
        res_explicit = analyzer.compute_critical_path(target_unit_index=0)
        self.assertEqual(res_explicit.target_unit_index, 0)
        self.assertEqual(res_explicit.target_selection_mode, "explicit_flag")

    def test_incomplete_build_session(self):
        """Test handling of session where compilation is interrupted (unit starts but never finishes)."""
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0"}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            # Interrupted before unit-finished!
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        self.assertEqual(res.target_unit_index, 0)
        self.assertEqual(len(res.segments), 1)
        self.assertIsNone(res.segments[0].full_finish_elapsed)

    def test_ambiguous_vs_clean_rebuild_causes(self):
        """Verify that 'new' units are not flagged ambiguous, but dirty units with generic causes are."""
        log = (
            '{"reason":"build-started"}\n'
            # Unit 0 is a clean build (status new)
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"new"}\n'
            # Unit 1 is a clean build (status new) depending on 0
            '{"reason":"unit-registered","index":1,"package_id":"lib1#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"new"}\n'
            # Unit 2 is dirty with 'nothing-obvious' cause, depending on dirty Unit 3
            '{"reason":"unit-registered","index":3,"package_id":"upstream#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":3,"status":"dirty","cause":{"dirty_reason":"rustc-changed"}}\n'
            '{"reason":"unit-registered","index":2,"package_id":"app#0.1.0","dependencies":[3]}\n'
            '{"reason":"unit-fingerprint","index":2,"status":"dirty","cause":{"dirty_reason":"nothing-obvious"}}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()

        # Clean units must not be ambiguous
        self.assertFalse(chains[0].is_ambiguous)
        self.assertEqual(chains[0].chain_category, "fresh_build")
        self.assertFalse(chains[1].is_ambiguous)
        self.assertEqual(chains[1].chain_category, "fresh_build")

        # Unit 2 is dirty with non-specific cause while dependency rebuilt -> ambiguous!
        self.assertTrue(chains[2].is_ambiguous)
        self.assertEqual(chains[2].chain_category, "ambiguous")
        self.assertIn("nothing-obvious", chains[2].ambiguity_reason)

    def test_invalid_timestamp_ordering_diagnostic(self):
        """Test that inverted timestamps trigger diagnostics rather than being silently hidden."""
        # Unit 1 starts at 0.10, but unblocker supposedly unblocked it at 0.20
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"d0#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.10}\n'  # starts BEFORE unblock!
            '{"reason":"unit-finished","index":1,"elapsed":0.30}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        self.assertTrue(res.segments[1].timestamp_inconsistent)
        self.assertTrue(any("Timestamp anomaly" in d for d in res.diagnostics))

    def test_cyclic_and_missing_rebuild_pointers(self):
        """Test defensive handling of corrupted/cyclic dirty-reason dependency pointers."""
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-registered","index":0,"package_id":"a#0.1.0"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"b#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"dirty","cause":{"dirty_reason":"unit-dependency-info-changed","unit":1}}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"unit-dependency-info-changed","unit":0}}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()

        self.assertTrue(chains[0].is_ambiguous)
        self.assertEqual(chains[0].chain_category, "broken_reference")
        self.assertIn("Cycle detected", chains[0].ambiguity_reason)

    def test_explicit_dirty_reason_chain_and_stale_path(self):
        """Test that full stale filesystem paths and mtimes are preserved."""
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-registered","index":0,"package_id":"leaf#0.1.0"}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"dirty","cause":{'
            '"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale_item":"changed-file",'
            '"stale":"/path/to/leaf/src/lib.rs","reference":"/path/to/dep-file","stale_mtime":12345.6}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"mid#0.1.0","dependencies":[0]}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"unit-dependency-info-changed","unit":0}}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        chains = analyzer.trace_rebuild_reasons()

        chain1 = chains[1]
        self.assertEqual(chain1.full_chain, [1, 0])
        self.assertEqual(chain1.root_cause_index, 0)
        self.assertEqual(chain1.stale_path, "/path/to/leaf/src/lib.rs")
        self.assertIn("/path/to/leaf/src/lib.rs", chain1.root_cause_summary)
        self.assertFalse(chain1.is_ambiguous)

    def test_json_serialization_round_trip(self):
        """Test that serialize_analysis emits pure JSON with no Python repr strings or lost numbers."""
        log = (
            '{"reason":"build-started","run_id":"r123","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.01}\n'
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0","target":{"name":"core","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"new"}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        rebuild_chains = analyzer.trace_rebuild_reasons()
        chain_res = analyzer.compute_critical_path()

        raw_dict = serialize_analysis(parser, rebuild_chains, chain_res)
        json_str = json.dumps(raw_dict, indent=2)

        # Verify no Python repr artifacts like '<UnblockEvent' or '<UnitRecord'
        self.assertNotIn("<", json_str)
        self.assertNotIn(">", json_str)

        # Verify round-trip parse
        loaded = json.loads(json_str)
        self.assertEqual(loaded["session"]["run_id"], "r123")
        self.assertEqual(loaded["units"]["0"]["name"], "core")
        self.assertEqual(loaded["unblocking_chain"]["target_unit_index"], 0)
        self.assertAlmostEqual(loaded["unblocking_chain"]["total_chain_duration"], 0.19)


if __name__ == "__main__":
    unittest.main()
