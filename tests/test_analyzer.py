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
    parse_package_id,
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
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"dep","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"requested":true,"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.30,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.35}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.60}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()
        self.assertEqual(res.status, "complete")

        self.assertEqual(res.target_unit_index, 1)
        self.assertEqual(len(res.segments), 2)

        seg0, seg1 = res.segments[0], res.segments[1]
        self.assertEqual(seg0.unit_index, 0)
        self.assertEqual(seg0.phase_unblocked, "finish")
        self.assertAlmostEqual(seg0.execution_duration, 0.20)
        self.assertIsNone(seg0.ready_to_start_delay)

        self.assertEqual(seg1.unit_index, 1)
        self.assertEqual(seg1.unblocked_by_index, 0)
        self.assertEqual(seg1.unblocked_by_kind, "finish")
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.05)
        self.assertAlmostEqual(seg1.execution_duration, 0.25)

        self.assertAlmostEqual(res.total_execution_duration, 0.45)
        self.assertAlmostEqual(res.total_ready_to_start_delays, 0.05)
        self.assertAlmostEqual(res.total_chain_duration, 0.50)

    def test_two_parallel_branches(self):
        """Test two independent parallel branches joining at a final binary."""
        # Branch 1: Unit 0 -> Unit 1
        # Branch 2: Unit 2
        # Target: Unit 3 depends on [1, 2].
        # Unit 2 finishes late at 0.50 and is the unblocker of Unit 3.
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"a#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"a","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"b#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"b","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":2,"package_id":"c#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"c","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":3,"package_id":"target#0.1.0","dependencies":[1,2],"requested":true,"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"target","kind":"bin"}}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-started","index":2,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.25}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.40}\n'
            '{"reason":"unit-finished","index":2,"elapsed":0.50,"unblocked":[3]}\n'
            '{"reason":"unit-started","index":3,"elapsed":0.52}\n'
            '{"reason":"unit-finished","index":3,"elapsed":0.70}\n'
        )
        parser = BuildAnalysisParser(strict=True).parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()

        # The observed unblocking chain should follow Unit 2 (the final unblocker) -> Unit 3
        self.assertEqual(parser.validation_errors, [])
        self.assertEqual({i: u.dependencies for i, u in parser.units.items()}, {0: [], 1: [0], 2: [], 3: [1, 2]})
        self.assertEqual(res.status, "complete")
        self.assertEqual(res.target_unit_index, 3)
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.segments[0].unit_index, 2)
        self.assertEqual(res.segments[1].unit_index, 3)
        self.assertEqual(res.segments[1].unblocked_by_index, 2)
        self.assertEqual(res.segments[1].unblocker_relationship, "direct")
        self.assertAlmostEqual(res.segments[0].execution_duration, .45)
        self.assertAlmostEqual(res.segments[1].execution_duration, .18)
        self.assertIsNone(res.segments[0].ready_to_start_delay)
        self.assertAlmostEqual(res.segments[1].ready_to_start_delay, .02)
        self.assertAlmostEqual(res.total_execution_duration, .63)
        self.assertAlmostEqual(res.total_ready_to_start_delays, .02)
        self.assertAlmostEqual(res.total_chain_duration, .65)

    def test_multiple_dependencies_joining_unblocker(self):
        """Verify that when multiple dependencies finish, only the final unblocker is recorded."""
        # Unit 2 depends on 0 and 1.
        # 0 finishes at 0.20, does not unblock 2.
        # 1 finishes at 0.30, unblocks 2.
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"d0#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"d0","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"d1#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"d1","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":2,"package_id":"target#0.1.0","dependencies":[0,1],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"target","kind":"lib"}}\n'
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
        self.assertEqual(res.status, "complete")

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
            '{"reason":"unit-registered","index":0,"package_id":"lib#0.1.0","target":{"name":"lib","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"target":{"name":"app","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-rmeta-finished","index":0,"elapsed":0.40,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.50}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.70}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.90}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()
        self.assertEqual(res.status, "complete")

        self.assertEqual(len(res.segments), 2)
        seg0, seg1 = res.segments[0], res.segments[1]

        # Unit 0 contributed up to rmeta
        self.assertEqual(seg0.unit_index, 0)
        self.assertEqual(seg0.phase_unblocked, "rmeta")
        self.assertAlmostEqual(seg0.start_elapsed, 0.10)
        self.assertAlmostEqual(seg0.effective_end_elapsed, 0.40)
        self.assertAlmostEqual(seg0.execution_duration, 0.30)
        self.assertIsNone(seg0.ready_to_start_delay)

        # Unit 1 unblocked by rmeta
        self.assertEqual(seg1.unit_index, 1)
        self.assertEqual(seg1.unblocked_by_index, 0)
        self.assertEqual(seg1.unblocked_by_kind, "rmeta")
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.10)
        self.assertAlmostEqual(seg1.execution_duration, 0.40)

        self.assertAlmostEqual(res.total_execution_duration, 0.70)
        self.assertAlmostEqual(res.total_ready_to_start_delays, 0.10)
        self.assertAlmostEqual(res.total_chain_duration, 0.80)

    def test_full_artifact_dependency(self):
        """Test that linkable targets (bin) require full completion of dependencies."""
        # Unit 0 is lib, Unit 1 is bin. Unit 1 cannot start on rmeta alone.
        log = (
            '{"reason":"build-started"}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"lib#0.1.0","target":{"name":"lib","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"cli#0.1.0","dependencies":[0],"target":{"name":"cli","kind":"bin"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.10}\n'
            '{"reason":"unit-rmeta-finished","index":0,"elapsed":0.30}\n'  # does NOT unblock cli!
            '{"reason":"unit-finished","index":0,"elapsed":0.50,"unblocked":[1]}\n'  # unblocks cli on full finish
            '{"reason":"unit-started","index":1,"elapsed":0.55}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.80}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()
        self.assertEqual(res.status, "complete")

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
            '{"reason":"unit-registered","index":5,"package_id":"string_parser#0.1.0","target":{"name":"sp","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-registered","index":4,"package_id":"orchestrator#0.1.0","dependencies":[5],"target":{"name":"orch","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-registered","index":0,"package_id":"cli_app#0.1.0","dependencies":[4],"target":{"name":"cli","kind":"bin"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
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
        self.assertEqual(res.status, "complete")

        # Target 0 was unblocked by 5 (synthetic edge), which in turn was unblocked at root
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.segments[0].unit_index, 5)
        self.assertEqual(res.segments[1].unit_index, 0)
        self.assertEqual(res.segments[1].unblocked_by_index, 5)
        self.assertEqual(res.segments[1].unblocker_relationship, "inferred_synthetic")

    def test_measures_gap_without_claiming_jobserver_cause(self):
        """The recorded transition measures a gap, not why the scheduler delayed it."""
        # Unit 0 finishes at 0.20, unblocking Unit 1.
        # Unit 1 starts at 0.72. Ready delay = 0.52.
        log = (
            '{"reason":"build-started","jobs":1}\n'
            '{"reason":"unit-graph-finished","elapsed":0.00}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"dep","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
            '{"reason":"unit-started","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","index":0,"elapsed":0.20,"unblocked":[1]}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.72}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.90}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()
        self.assertEqual(res.status, "complete")

        seg1 = res.segments[1]
        self.assertAlmostEqual(seg1.ready_to_start_delay, 0.52)
        self.assertAlmostEqual(seg1.execution_duration, 0.18)

    def test_fresh_dependencies_handling(self):
        """Test that units with only fresh dependencies are handled without failing."""
        log = (
            '{"reason":"build-started","jobs":4}\n'
            '{"reason":"unit-graph-finished","elapsed":0.02}\n'
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"dep","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"fresh"}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"dirty","cause":{"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale_item":"changed-file","stale":"src/main.rs"}}\n'
            '{"reason":"unit-started","index":1,"elapsed":0.10}\n'
            '{"reason":"unit-finished","index":1,"elapsed":0.40}\n'
        )
        parser = BuildAnalysisParser().parse_str(log)
        analyzer = CriticalPathAnalyzer(parser)
        res = analyzer.compute_critical_path()
        self.assertEqual(res.status, "complete")

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
            '{"reason":"unit-registered","index":0,"package_id":"dep#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"dep","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"app1#0.1.0","requested":true,"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app1","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app2#0.1.0","requested":true,"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app2","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"core","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"core#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"core","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"new"}\n'
            # Unit 1 is a clean build (status new) depending on 0
            '{"reason":"unit-registered","index":1,"package_id":"lib1#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"lib1","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":1,"status":"new"}\n'
            # Unit 2 is dirty with 'nothing-obvious' cause, depending on dirty Unit 3
            '{"reason":"unit-registered","index":3,"package_id":"upstream#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"upstream","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":3,"status":"dirty","cause":{"dirty_reason":"rustc-changed"}}\n'
            '{"reason":"unit-registered","index":2,"package_id":"app#0.1.0","dependencies":[3],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"d0#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"d0","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"app#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"app","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"a#0.1.0","dependencies":[1],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"a","kind":"lib"}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"b#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"b","kind":"lib"}}\n'
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
            '{"reason":"unit-registered","index":0,"package_id":"leaf#0.1.0","mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"leaf","kind":"lib"}}\n'
            '{"reason":"unit-fingerprint","index":0,"status":"dirty","cause":{'
            '"dirty_reason":"fs-status-outdated","fs_status":"stale-item","stale_item":"changed-file",'
            '"stale":"/path/to/leaf/src/lib.rs","reference":"/path/to/dep-file","stale_mtime":12345.6}}\n'
            '{"reason":"unit-registered","index":1,"package_id":"mid#0.1.0","dependencies":[0],"mode":"build","platform":"x86_64-unknown-linux-gnu","target":{"name":"mid","kind":"lib"}}\n'
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
        self.assertEqual(parse_package_id("registry+https://github.com/rust-lang/crates.io-index#serde@1.0.228"),
                         ("serde", "1.0.228"))
        log = (
            '{"reason":"build-started","run_id":"r123","jobs":4}\n'
            '{"reason":"unit-graph-finished","run_id":"r123","elapsed":0.01}\n'
            '{"reason":"unit-registered","run_id":"r123","index":0,"package_id":"core#0.1.0","target":{"name":"core","kind":"lib"},"mode":"build","platform":"x86_64-unknown-linux-gnu"}\n'
            '{"reason":"unit-fingerprint","run_id":"r123","index":0,"status":"new"}\n'
            '{"reason":"unit-started","run_id":"r123","index":0,"elapsed":0.05}\n'
            '{"reason":"unit-finished","run_id":"r123","index":0,"elapsed":0.20}\n'
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
        self.assertAlmostEqual(loaded["unblocking_chain"]["total_chain_duration"], 0.15)


def registration(index, name="crate", kind="lib", mode="build", dependencies=(), requested=False):
    return dict(reason="unit-registered", index=index, package_id=f"{name}#0.1.0",
                target={"name": name, "kind": kind}, mode=mode, platform="x86_64-unknown-linux-gnu",
                dependencies=list(dependencies), requested=requested)


def event(reason, index, elapsed, **fields):
    return dict(reason=reason, index=index, elapsed=elapsed, **fields)


def parse_events(*events, strict=False):
    return BuildAnalysisParser(strict=strict).parse_str("\n".join(json.dumps(e) for e in events))


class TestAuditBoundaries(unittest.TestCase):
    # Source contracts and fixture intent are mapped in FINDINGS.md's test notes.
    def test_real_workspace_final_unblocker_relationship(self):
        for final in ("orchestrator", "string_parser"):
            with self.subTest(final=final):
                string_finish = .07 if final == "orchestrator" else .09
                regs = [registration(0, "core_util"), registration(1, "math_engine", dependencies=[0]),
                        registration(2, "string_parser", dependencies=[0]),
                        registration(3, "orchestrator", dependencies=[1, 2]),
                        registration(4, "cli_app", kind="bin", dependencies=[3], requested=True),
                        registration(5, "leaf_tool", kind="bin", dependencies=[2])]
                times = [event("unit-started", 0, .01), event("unit-rmeta-finished", 0, .02, unblocked=[1, 2]),
                         event("unit-finished", 0, .03), event("unit-started", 1, .021),
                         event("unit-started", 2, .022), event("unit-rmeta-finished", 1, .04),
                         event("unit-rmeta-finished", 2, .05, unblocked=[3]), event("unit-started", 3, .051),
                         event("unit-finished", 1, .06),
                         event("unit-finished", 3, .08, unblocked=[4] if final == "orchestrator" else []),
                         event("unit-finished", 2, string_finish, unblocked=[5] if final == "orchestrator" else [4, 5]),
                         event("unit-started", 4, max(.08, string_finish) + .001), event("unit-finished", 4, .2),
                         event("unit-started", 5, string_finish + .001), event("unit-finished", 5, .15)]
                parser = parse_events(*regs, *sorted(times, key=lambda e: e["elapsed"]), strict=True)
                chain = CriticalPathAnalyzer(parser).compute_critical_path(4)
                self.assertEqual(chain.status, "complete")
                self.assertEqual(chain.segments[-1].unblocked_by_index, 3 if final == "orchestrator" else 2)
                self.assertEqual(chain.segments[-1].unblocker_relationship,
                                 "direct" if final == "orchestrator" else "inferred_synthetic")
                report = format_text_report(parser, CriticalPathAnalyzer(parser), {}, chain)
                self.assertIn("(direct)" if final == "orchestrator" else "(inferred synthetic)", report)

    def test_library_crate_types_are_not_recoverable_from_registration(self):
        # Actual 1.99 registrations for three manifests with different crate-type
        # arrays. See FINDINGS.md for the capture and normalization details.
        fixture = Path(__file__).parent / "fixtures" / "library_registrations.jsonl"
        parser = BuildAnalysisParser(strict=True).parse_file(fixture)
        for name in ("rlib_output", "cdylib_output", "staticlib_output"):
            with self.subTest(name=name):
                unit = next(u for u in parser.units.values() if u.name == name)
                self.assertEqual(unit.target_kind, "lib")
                self.assertIsNone(unit.requires_upstream_objects)
                direct, = unit.effective_dependencies
                self.assertEqual((direct.required_artifact, direct.certainty), ("unknown", "unknown"))
                self.assertTrue(any(f"Unit {unit.index}:" in d for d in parser.inference_diagnostics))

    def test_mode_and_target_rules_are_separate(self):
        for kind, mode in (("bin", "build"), ("lib", "test"), ("lib", "check"), ("lib", "doctest")):
            with self.subTest(kind=kind, mode=mode):
                parser = parse_events(registration(0), registration(1, dependencies=[0]),
                                      registration(2, kind=kind, mode=mode, dependencies=[1]))
                direct = next(d for d in parser.units[2].effective_dependencies if d.dep_index == 1)
                synthetic = next(d for d in parser.units[2].effective_dependencies if d.dep_index == 0)
                self.assertEqual(direct.required_artifact, "all")
                self.assertEqual(parser.units[2].requires_upstream_objects, None if mode == "check" else True)
                self.assertEqual(synthetic.certainty, "unknown" if mode == "check" else "inferred")
        parser = parse_events(registration(0, mode="check"), registration(1, dependencies=[0]))
        self.assertEqual(parser.units[1].effective_dependencies[0].required_artifact, "all")

    def test_metadata_unblock_resolves_artifact_requirement(self):
        parser = parse_events(registration(0), registration(1, dependencies=[0]),
                              event("unit-started", 0, .1), event("unit-rmeta-finished", 0, .2, unblocked=[1]),
                              event("unit-started", 1, .21), event("unit-finished", 1, .3),
                              event("unit-finished", 0, .4), strict=True)
        dep, = parser.units[1].effective_dependencies
        self.assertEqual((dep.required_artifact, dep.certainty), ("metadata", "inferred"))
        result = CriticalPathAnalyzer(parser).compute_critical_path(1)
        self.assertEqual(result.segments[0].effective_end_elapsed, .2)
        self.assertAlmostEqual(result.total_chain_duration, .2)
        self.assertIn("after the selected target", " ".join(result.unreconstructed_notes))

    def test_synthetic_overlap_keeps_direct_edge_and_upgrades_to_all(self):
        parser = parse_events(registration(0), registration(1, dependencies=[0]),
                              registration(2, kind="bin", dependencies=[1, 0, 0]))
        self.assertEqual(parser.units[2].dependencies, [1, 0, 0])
        deps = parser.units[2].effective_dependencies
        self.assertEqual(len(deps), 2)
        self.assertTrue(all(not d.is_synthetic and d.required_artifact == "all" for d in deps))

    def test_filtered_binary_keeps_transitive_candidates_uncertain(self):
        # An integration test also registers its package's binary for execution.
        # enqueue may filter that binary, but still walks its original dependencies.
        regs = [registration(2, "core"), registration(1, "support", dependencies=[2]),
                registration(0, "app", kind="bin", dependencies=[1]),
                registration(3, "app", kind="test", mode="test", dependencies=[0, 1], requested=True)]
        parser = parse_events(*regs, strict=True)
        self.assertEqual(parser.units[3].dependencies, [0, 1])
        self.assertEqual(parser.units[0].dependencies, [1])
        self.assertEqual(parser.units[1].dependencies, [2])
        edges = {d.dep_index: d for d in parser.units[3].effective_dependencies}
        self.assertEqual((edges[0].required_artifact, edges[0].certainty, edges[0].is_synthetic),
                         ("unknown", "unknown", False))
        self.assertEqual((edges[1].required_artifact, edges[1].certainty, edges[1].is_synthetic),
                         ("all", "known", False))
        # All is the artifact requirement if this candidate is present; it does
        # not override the model's conservative certainty about queue membership.
        self.assertEqual((edges[2].required_artifact, edges[2].certainty, edges[2].is_synthetic),
                         ("all", "unknown", True))
        self.assertTrue(all(u.unblocked_by is None for u in parser.units.values()))
        diagnostic = next(d for d in parser.inference_diagnostics if d.startswith("Unit 3:"))
        self.assertIn("artifact-dependency flags", diagnostic)
        self.assertIn("not an exact queue graph", diagnostic)

        times = [event("unit-started", 2, .1), event("unit-rmeta-finished", 2, .2, unblocked=[1]),
                 event("unit-started", 1, .21), event("unit-finished", 1, .4),
                 event("unit-finished", 2, .5, unblocked=[0, 3]), event("unit-started", 0, .51),
                 event("unit-started", 3, .52), event("unit-finished", 3, .7),
                 event("unit-finished", 0, .8)]
        parser = parse_events(*regs, *times, strict=True)
        analyzer = CriticalPathAnalyzer(parser)
        chain = analyzer.compute_critical_path()
        self.assertEqual(chain.status, "complete")
        self.assertEqual([s.unit_index for s in chain.segments], [2, 3])
        self.assertEqual(chain.segments[-1].unblocker_relationship, "unknown")
        self.assertEqual(chain.segments[-1].unblocked_by_index, 2)
        self.assertIn(diagnostic, format_text_report(parser, analyzer, {}, chain))

    def test_documentation_scraping_keeps_a_binary_dependency(self):
        parser = parse_events(registration(0, kind="bin", mode="docscrape"),
                              registration(1, mode="doc", dependencies=[0]), strict=True)
        edge, = parser.units[1].effective_dependencies
        self.assertEqual((edge.required_artifact, edge.certainty), ("all", "known"))

    def test_mixed_sessions_are_excluded_including_future_events(self):
        events = [dict(registration(0), run_id="first"), dict(registration(1), run_id="second"),
                  dict(event("unit-started", 0, .1), run_id="second"),
                  dict(reason="future-event", run_id="second")]
        with self.assertRaisesRegex(LogParseError, "Mixed sessions"):
            parse_events(*events, strict=True)
        parser = parse_events(*events)
        self.assertEqual(set(parser.units), {0})
        self.assertIsNone(parser.units[0].start_elapsed)
        self.assertEqual(len(parser.malformed_lines), 3)
        result = CriticalPathAnalyzer(parser).compute_critical_path()
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(serialize_analysis(parser, {}, result)["input_valid"])
        self.assertIn("Mixed sessions", format_text_report(parser, CriticalPathAnalyzer(parser), {}, result))

    def test_consistent_session_identity_accepts_known_and_future_events(self):
        for run_id in (None, "session"):
            with self.subTest(run_id=run_id):
                events = [registration(0, requested=True), dict(reason="future-event"),
                          event("unit-started", 0, .1), event("unit-finished", 0, .3)]
                if run_id is not None:
                    events = [dict(record, run_id=run_id) for record in events]
                for strict in (False, True):
                    parser = parse_events(*events, strict=strict)
                    self.assertEqual(parser.session.run_id, run_id)
                    self.assertEqual(parser.malformed_lines, [])
                    self.assertEqual(parser.unrecognized_events, ["future-event"])
                    chain = CriticalPathAnalyzer(parser).compute_critical_path()
                    self.assertEqual(chain.status, "complete")
                    self.assertAlmostEqual(chain.total_execution_duration, .2)

    def test_identified_sessions_exclude_missing_or_invalid_ids_before_mutation(self):
        for identity in ({}, {"run_id": ""}, {"run_id": None}, {"run_id": 0}, {"run_id": False}):
            for record in (registration(1, dependencies=[0]), event("unit-started", 0, .05),
                           dict(reason="future-event")):
                with self.subTest(identity=identity, reason=record["reason"]):
                    events = [dict(registration(0), run_id="session"), dict(record, **identity),
                              dict(event("unit-started", 0, .1), run_id="session"),
                              dict(event("unit-finished", 0, .3), run_id="session")]
                    with self.assertRaisesRegex(LogParseError, "run_id"):
                        parse_events(*events, strict=True)
                    parser = parse_events(*events)
                    self.assertEqual(parser.session.run_id, "session")
                    self.assertEqual(set(parser.units), {0})
                    self.assertEqual(parser.units[0].dependencies, [])
                    self.assertEqual(parser.units[0].start_elapsed, .1)
                    self.assertIsNone(parser.units[0].unblocked_by)
                    self.assertEqual(parser.unrecognized_events, [])
                    self.assertEqual(len(parser.malformed_lines), 1)
                    analyzer = CriticalPathAnalyzer(parser)
                    chain = analyzer.compute_critical_path()
                    self.assertEqual(chain.status, "incomplete")
                    data = serialize_analysis(parser, {}, chain)
                    self.assertFalse(data["input_valid"])
                    self.assertIn("run_id", data["diagnostics"]["malformed_lines"][0][2])
                    self.assertIn("run_id", format_text_report(parser, analyzer, {}, chain))

    def test_unidentified_prefix_cannot_be_adopted_by_a_later_session(self):
        events = [registration(0), dict(registration(1), run_id="later"),
                  dict(reason="future-event", run_id="later"),
                  event("unit-started", 0, .1), event("unit-finished", 0, .3)]
        with self.assertRaisesRegex(LogParseError, "unidentified prefix"):
            parse_events(*events, strict=True)
        parser = parse_events(*events)
        self.assertIsNone(parser.session.run_id)
        self.assertEqual(set(parser.units), {0})
        self.assertEqual(parser.unrecognized_events, [])
        self.assertEqual(len(parser.malformed_lines), 2)
        self.assertAlmostEqual(parser.units[0].duration, .2)
        # A rejected first payload must not choose the identity of valid records.
        malformed = dict(reason="unit-started", index=0, run_id="rejected")
        parser = parse_events(malformed, registration(0))
        self.assertIsNone(parser.session.run_id)
        self.assertEqual(set(parser.units), {0})

    def test_duplicate_and_conflicting_registrations_preserve_first_record(self):
        for duplicate in (registration(0), registration(0, name="different")):
            with self.subTest(duplicate=duplicate):
                with self.assertRaisesRegex(LogParseError, "registration"):
                    parse_events(registration(0), duplicate, strict=True)
                parser = parse_events(registration(0), event("unit-started", 0, .1), duplicate)
                self.assertEqual(parser.units[0].name, "crate")
                self.assertEqual(parser.units[0].start_elapsed, .1)
                self.assertEqual(len(parser.malformed_lines), 1)

    def test_invalid_fields_and_references_fail_strict_and_mark_recovery(self):
        invalid = [dict(registration(1), dependencies=["0"]), dict(registration(1), dependencies=[-1]),
                   dict(registration(1), dependencies=[True]),
                   dict(registration(1), index=1.5), dict(registration(1), index=2**64),
                   dict(reason="unit-started", index=0), event("unit-started", 0, float("nan")),
                   event("unit-started", 0, float("inf")), event("unit-started", -1, .1),
                   dict(reason="unit-fingerprint", index=0, status="dirty",
                        cause={"dirty_reason":"unit-dependency-info-changed", "unit":"1"}),
                   dict(registration(1), dependencies=[99])]
        for bad in invalid:
            with self.subTest(bad=bad):
                with self.assertRaises(LogParseError):
                    parse_events(registration(0), bad, strict=True)
                parser = parse_events(registration(0), bad)
                self.assertTrue(parser.malformed_lines or parser.validation_errors)

    def test_out_of_order_registration_and_timing_recover_without_data_loss(self):
        parser = parse_events(event("unit-finished", 0, .3), event("unit-started", 0, .1), registration(0),
                              dict(reason="future-event", payload={"anything": True}), strict=True)
        self.assertEqual(parser.units[0].duration, .3 - .1)
        self.assertTrue(parser.units[0].registered)
        self.assertTrue(parser.parse_warnings)
        self.assertEqual(parser.unrecognized_events, ["future-event"])
        result = CriticalPathAnalyzer(parser).compute_critical_path()
        self.assertEqual(result.status, "complete")
        # features, requested, dependencies, and run_id are optional.
        reg = registration(1)
        for key in ("features", "requested", "dependencies"):
            reg.pop(key, None)
        unit = parse_events(reg, strict=True).units[1]
        self.assertEqual((unit.features, unit.dependencies, unit.requested), ([], [], False))

    def test_timestamp_conflicts_never_produce_reliable_totals(self):
        cases = [ [event("unit-started", 0, .2), event("unit-finished", 0, .1)],
                  [event("unit-started", 0, .2), event("unit-rmeta-finished", 0, .1)],
                  [event("unit-started", 0, .1), event("unit-finished", 0, .2), event("unit-rmeta-finished", 0, .3)],
                  [event("unit-started", 0, .1), event("unit-finished", 0, .2), event("unit-finished", 0, .3)],
                  [event("unit-started", 0, .1), event("unit-finished", 0, .2, unblocked=[1]),
                   event("unit-rmeta-finished", 0, .15, unblocked=[1])],
                  [event("unit-started", 0, .1), event("unit-finished", 0, .2, unblocked=[1]),
                   event("unit-started", 1, .15), event("unit-finished", 1, .3)] ]
        for times in cases:
            with self.subTest(times=times):
                with self.assertRaises(LogParseError):
                    parse_events(registration(0), registration(1, dependencies=[0]), *times, strict=True)
                parser = parse_events(registration(0), registration(1, dependencies=[0]), *times)
                chain = CriticalPathAnalyzer(parser).compute_critical_path(0)
                self.assertIn(chain.status, ("inconsistent", "incomplete"))
                self.assertIsNone(chain.total_chain_duration)

    def test_missing_registration_and_graph_cycles_are_reported(self):
        for events in ([event("unit-started", 0, .1)],
                       [registration(0, dependencies=[1]), registration(1, dependencies=[0])]):
            with self.subTest(events=events):
                with self.assertRaises(LogParseError):
                    parse_events(*events, strict=True)
                self.assertTrue(parse_events(*events).validation_errors)

    def test_partial_chain_keeps_known_segments_and_unknown_end(self):
        parser = parse_events(registration(0), registration(1, dependencies=[0], requested=True),
                              event("unit-started", 0, .1), event("unit-finished", 0, .2, unblocked=[1]),
                              event("unit-started", 1, .3))
        chain = CriticalPathAnalyzer(parser).compute_critical_path()
        self.assertEqual(chain.status, "incomplete")
        self.assertEqual(chain.target_selection_mode, "requested_incomplete")
        self.assertAlmostEqual(chain.segments[0].execution_duration, .1)
        self.assertIsNone(chain.segments[1].execution_duration)
        self.assertIsNone(chain.segments[1].effective_end_elapsed)
        self.assertIsNone(chain.total_execution_duration)
        self.assertIn("Unit 1", " ".join(chain.diagnostics))
        data = serialize_analysis(parser, {}, chain)
        self.assertEqual(json.loads(json.dumps(data, allow_nan=False)), data)
        self.assertIn("unknown", format_text_report(parser, CriticalPathAnalyzer(parser), {}, chain))

    def test_requested_target_selection_does_not_imply_session_completion(self):
        parser = parse_events(registration(0, requested=True), registration(1),
                              registration(2, requested=True), event("unit-started", 0, .1),
                              event("unit-finished", 0, .2), event("unit-started", 1, .1),
                              event("unit-finished", 1, .4), event("unit-started", 2, .15))
        analyzer = CriticalPathAnalyzer(parser)
        chain = analyzer.compute_critical_path()
        self.assertEqual((chain.target_unit_index, chain.target_selection_mode), (0, "requested_target"))
        self.assertIn("after the selected target", " ".join(chain.unreconstructed_notes))
        self.assertEqual(analyzer.compute_critical_path(2).status, "incomplete")
        fresh = parse_events(registration(0, requested=True), dict(reason="unit-fingerprint", index=0, status="fresh"))
        chain = CriticalPathAnalyzer(fresh).compute_critical_path(0)
        self.assertEqual((chain.target_unit_index, chain.status), (0, "not_executed"))
        self.assertEqual(CriticalPathAnalyzer(fresh).compute_critical_path().status, "no_execution")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            CriticalPathAnalyzer(fresh).compute_critical_path(99)
        unstarted = CriticalPathAnalyzer(parse_events(registration(0, requested=True))).compute_critical_path(0)
        self.assertEqual(unstarted.status, "incomplete")

    def test_terminal_fingerprint_ambiguity_and_broken_references(self):
        regs = [registration(0, dependencies=[1]), registration(1, dependencies=[2]),
                registration(2, dependencies=[3, 4]), registration(3), registration(4)]
        reasons = [dict(reason="unit-fingerprint", index=i, status="dirty", cause=cause)
                   for i, cause in enumerate([{"dirty_reason":"unit-dependency-info-changed", "unit":1},
                                             {"dirty_reason":"fs-status-outdated", "fs_status":"stale-dep-fingerprint", "unit":2},
                                             {"dirty_reason":"nothing-obvious"},
                                             {"dirty_reason":"rustc-changed"}, {"dirty_reason":"features-changed", "old":"", "new":"feature"}])]
        chain = CriticalPathAnalyzer(parse_events(*regs, *reasons)).trace_rebuild_reasons()[0]
        self.assertEqual((chain.full_chain, chain.chain_category, chain.is_ambiguous), ([0, 1, 2], "ambiguous", True))
        self.assertIsNone(chain.root_cause_index)
        for status, dep, expected in (("fresh", 1, "unresolved_upstream"), ("dirty", 99, "broken_reference"),
                                      ("dirty", 2, "broken_reference")):
            with self.subTest(status=status, dep=dep):
                parser = parse_events(registration(0, dependencies=[1]), registration(1), registration(2),
                                      dict(reason="unit-fingerprint", index=0, status="dirty",
                                           cause={"dirty_reason":"unit-dependency-info-changed", "unit":dep}),
                                      dict(reason="unit-fingerprint", index=1, status=status))
                self.assertEqual(CriticalPathAnalyzer(parser).trace_rebuild_reasons()[0].chain_category, expected)
        for cause, category in (({"dirty_reason":"forced"}, "forced_rebuild"),
                                ({"dirty_reason":"fs-status-outdated", "fs_status":"stale"}, "unresolved_local"),
                                ({"dirty_reason":"future-cause"}, "unresolved_local")):
            parser = parse_events(registration(0), dict(reason="unit-fingerprint", index=0, status="dirty", cause=cause))
            result = CriticalPathAnalyzer(parser).trace_rebuild_reasons()[0]
            self.assertEqual(result.chain_category, category)
            self.assertTrue(result.is_ambiguous)

    def test_dirty_schema_preserves_local_file_details_and_unknown_variants(self):
        cases = [
            {"dirty_reason":"fs-status-outdated", "fs_status":"stale-item", "stale_item":"changed-file",
             "stale":"/workspace/src/lib.rs", "reference":"/workspace/dep-info", "stale_mtime":2000.5, "reference_mtime":1000.25},
            {"dirty_reason":"fs-status-outdated", "fs_status":"stale-item", "stale_item":"changed-checksum",
             "source":"/workspace/src/lib.rs", "stored_checksum":"sha256=" + "a" * 64, "new_checksum":"sha256=" + "b" * 64},
            {"dirty_reason":"fs-status-outdated", "fs_status":"stale-item", "stale_item":"missing-file", "path":"/workspace/build.rs"},
            {"dirty_reason":"fs-status-outdated", "fs_status":"stale-item", "stale_item":"changed-env",
             "var":"BUILD_INPUT", "previous":"old", "current":"new"},
            {"dirty_reason":"checksum-use-changed", "old":False},
            {"dirty_reason":"fingerprint-method-changed", "old":"mtime", "new":"content"},
        ]
        for cause in cases:
            with self.subTest(cause=cause):
                parser = parse_events(registration(0), dict(reason="unit-fingerprint", index=0, status="dirty", cause=cause), strict=True)
                chain = CriticalPathAnalyzer(parser).trace_rebuild_reasons()[0]
                self.assertEqual(chain.chain_category, "local_invalidation")
                self.assertEqual(parser.units[0].parsed_cause.details, cause)
                if cause.get("stale_item") == "changed-file":
                    parsed = parser.units[0].parsed_cause
                    self.assertEqual((parsed.stale_path, parsed.reference_path, parsed.stale_mtime, parsed.reference_mtime),
                                     (cause["stale"], cause["reference"], 2000.5, 1000.25))
        parser = parse_events(registration(0), event("unit-started", 0, float("nan")))
        result = CriticalPathAnalyzer(parser).compute_critical_path()
        data = serialize_analysis(parser, {}, result)
        self.assertEqual(json.loads(json.dumps(data, allow_nan=False)), data)


if __name__ == "__main__":
    unittest.main()
