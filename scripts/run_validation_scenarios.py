#!/usr/bin/env python3
"""Automated, reproducible validation harness for Cargo -Zbuild-analysis critical-path prototype.

Executes scenarios against an isolated copy of test_workspace:
1. Initial clean build
2. No-change rebuild
3. Leaf modification
4. Shared dependency modification
5. Concurrency comparison: -j 1 vs -j 8

Performs rigorous, fail-closed validation:
- Matches exact run IDs via log directory diffing (never selects stale logs).
- Verifies exact expected counts of executed units.
- Verifies unit start times and durations against Cargo's HTML report.
- Guarantees tracked repository source files remain unmodified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from build_trace import (
    BuildAnalysisParser,
    CriticalPathAnalyzer,
    format_text_report,
    serialize_analysis,
)

ORIGINAL_WORKSPACE = REPO_ROOT / "test_workspace"
OUTPUT_DIR = REPO_ROOT / "validation_output"
CARGO_LOG_DIR = Path.home() / ".cargo" / "log"


def run_cmd(cmd: List[str], cwd: Path, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env["RUSTC_BOOTSTRAP"] = "1"
    full_env["CARGO_BUILD_ANALYSIS_ENABLED"] = "true"
    if env:
        full_env.update(env)

    res = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Command failed ({' '.join(cmd)}) in {cwd}:", file=sys.stderr)
        print("STDOUT:\n", res.stdout, file=sys.stderr)
        print("STDERR:\n", res.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed with code {res.returncode}")
    return res


def extract_cargo_timing_unit_data(html_path: Path) -> List[Dict[str, Any]]:
    """Extract UNIT_DATA array embedded in cargo-timing-*.html."""
    if not html_path.exists():
        raise FileNotFoundError(f"Expected Cargo timing HTML report not found at: {html_path}")

    content = html_path.read_text(encoding="utf-8")
    m = re.search(r"const UNIT_DATA = (\[.*?\]);", content, re.DOTALL)
    if not m:
        raise ValueError(f"Could not locate 'const UNIT_DATA' in Cargo timing HTML: {html_path}")

    try:
        return json.loads(m.group(1))
    except Exception as e:
        raise ValueError(f"Failed to parse UNIT_DATA JSON from {html_path}: {e}") from e


def run_scenario(
    name: str,
    ws_dir: Path,
    prepare_fn,
    build_args: List[str],
    expected_executed_units: int,
    expected_dirty_units: int,
    expected_fresh_units: int,
    expected_new_units: int,
) -> Dict[str, Any]:
    print(f"\n{'=' * 70}")
    print(f"Scenario: {name}")
    print(f"{'=' * 70}")

    prepare_fn(ws_dir)

    # Snapshot existing logs in ~/.cargo/log to detect the exact new file
    CARGO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    before_logs: Set[Path] = set(CARGO_LOG_DIR.glob("*.jsonl"))

    cargo_cmd = [
        "cargo", "build",
        "--manifest-path", str(ws_dir / "Cargo.toml"),
        "-Zbuild-analysis",
    ] + build_args
    print(f"Executing: {' '.join(cargo_cmd)}")
    run_cmd(cargo_cmd, cwd=ws_dir)

    # Capture the exact newly created log file
    after_logs: Set[Path] = set(CARGO_LOG_DIR.glob("*.jsonl"))
    new_logs = after_logs - before_logs
    if len(new_logs) != 1:
        raise RuntimeError(
            f"Expected exactly 1 new log file in {CARGO_LOG_DIR}, found {len(new_logs)}: "
            f"{[p.name for p in new_logs]}"
        )
    log_file = list(new_logs)[0]
    run_id = log_file.stem
    print(f"Captured exact session log: {log_file.name} ({log_file.stat().st_size} bytes)")

    # Run cargo report timings for this specific session
    timings_cmd = [
        "cargo", "report", "timings",
        "-Zbuild-analysis",
        "--id", run_id,
        "--manifest-path", str(ws_dir / "Cargo.toml"),
    ]
    run_cmd(timings_cmd, cwd=ws_dir)

    cargo_html_path = ws_dir / "target" / "cargo-timings" / f"cargo-timing-{run_id}.html"
    if not cargo_html_path.exists():
        raise FileNotFoundError(f"Cargo report timings did not produce expected file at: {cargo_html_path}")

    # Copy artifacts to validation_output
    scenario_dir = OUTPUT_DIR / name.lower().replace(" ", "_").replace("-", "_")
    scenario_dir.mkdir(parents=True, exist_ok=True)

    dest_log = scenario_dir / f"{name}_session.jsonl"
    shutil.copy2(log_file, dest_log)
    shutil.copy2(cargo_html_path, scenario_dir / f"{name}_cargo_timing.html")

    # Run our analyzer
    parser = BuildAnalysisParser(strict=True)
    parser.parse_file(dest_log)
    analyzer = CriticalPathAnalyzer(parser)
    rebuild_chains = analyzer.trace_rebuild_reasons()
    crit_path = analyzer.compute_critical_path()

    # Save reports
    text_report = format_text_report(parser, analyzer, rebuild_chains, crit_path)
    (scenario_dir / f"{name}_report.txt").write_text(text_report, encoding="utf-8")

    json_data = serialize_analysis(parser, rebuild_chains, crit_path)
    (scenario_dir / f"{name}_analysis.json").write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    # Verification of unit counts
    actual_executed = sum(1 for u in parser.units.values() if u.start_elapsed is not None)
    actual_dirty = sum(1 for u in parser.units.values() if u.status == "dirty")
    actual_fresh = sum(1 for u in parser.units.values() if u.status == "fresh")
    actual_new = sum(1 for u in parser.units.values() if u.status == "new")

    if actual_executed != expected_executed_units:
        raise AssertionError(
            f"Executed units mismatch for {name}: expected {expected_executed_units}, got {actual_executed}"
        )
    if actual_dirty != expected_dirty_units:
        raise AssertionError(
            f"Dirty units mismatch for {name}: expected {expected_dirty_units}, got {actual_dirty}"
        )
    if actual_fresh != expected_fresh_units:
        raise AssertionError(
            f"Fresh units mismatch for {name}: expected {expected_fresh_units}, got {actual_fresh}"
        )
    if actual_new != expected_new_units:
        raise AssertionError(
            f"New units mismatch for {name}: expected {expected_new_units}, got {actual_new}"
        )

    # Verification against Cargo HTML timing report
    cargo_timing_units = extract_cargo_timing_unit_data(cargo_html_path)
    if len(cargo_timing_units) != expected_executed_units:
        raise AssertionError(
            f"Cargo timing HTML reported {len(cargo_timing_units)} units, expected {expected_executed_units}"
        )

    agreement_records = []
    tolerance = 0.02
    for c_unit in cargo_timing_units:
        idx = c_unit["i"]
        c_name = c_unit.get("name")
        c_start = c_unit["start"]
        c_dur = c_unit["duration"]

        our_unit = parser.units.get(idx)
        if our_unit is None:
            raise AssertionError(f"Cargo unit index {idx} ({c_name}) not found in analyzer registry")

        if our_unit.start_elapsed is None or our_unit.duration is None:
            raise AssertionError(f"Unit {idx} has missing timestamps in analyzer output")

        our_start = round(our_unit.start_elapsed, 2)
        our_dur = round(our_unit.duration, 2)

        start_diff = abs(our_start - c_start)
        dur_diff = abs(our_dur - c_dur)

        if start_diff > tolerance:
            raise AssertionError(
                f"Start time divergence for unit {idx} ({c_name}): Cargo={c_start:.2f}s, Ours={our_start:.2f}s "
                f"(diff={start_diff:.3f}s > {tolerance}s)"
            )
        if dur_diff > tolerance:
            raise AssertionError(
                f"Duration divergence for unit {idx} ({c_name}): Cargo={c_dur:.2f}s, Ours={our_dur:.2f}s "
                f"(diff={dur_diff:.3f}s > {tolerance}s)"
            )

        agreement_records.append({
            "index": idx,
            "name": c_name,
            "cargo_start": c_start,
            "our_start": our_start,
            "cargo_duration": c_dur,
            "our_duration": our_dur,
            "start_diff": start_diff,
            "duration_diff": dur_diff,
            "agrees": True,
        })

    print(f"PASS: {name}")
    print(f"  Executed units: {actual_executed} (agrees 100% with Cargo timing report)")
    print(f"  Status breakdown: {actual_new} new, {actual_dirty} dirty, {actual_fresh} fresh")
    print(f"  Unblocking chain steps: {len(crit_path.segments)}")
    print(f"  Total chain duration: {crit_path.total_chain_duration:.3f}s "
          f"(exec: {crit_path.total_execution_duration:.3f}s, ready delay: {crit_path.total_ready_to_start_delays:.3f}s)")

    summary = {
        "scenario": name,
        "run_id": run_id,
        "total_units": len(parser.units),
        "executed_units": actual_executed,
        "dirty_units": actual_dirty,
        "fresh_units": actual_fresh,
        "new_units": actual_new,
        "chain_steps": len(crit_path.segments),
        "chain_duration": crit_path.total_chain_duration,
        "execution_duration": crit_path.total_execution_duration,
        "ready_to_start_delays": crit_path.total_ready_to_start_delays,
        "timing_agreement_units_compared": len(agreement_records),
        "timing_agreement_pct": 100.0,
        "agreement_records": agreement_records,
    }

    (scenario_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cargo_trace_val_") as temp_dir:
        temp_ws = Path(temp_dir) / "test_workspace"
        print(f"Creating isolated workspace copy at: {temp_ws}")
        shutil.copytree(ORIGINAL_WORKSPACE, temp_ws)

        # 1. Initial clean build
        def prep_clean(ws: Path):
            run_cmd(["cargo", "clean", "--manifest-path", str(ws / "Cargo.toml")], cwd=ws)

        res_clean = run_scenario(
            "1_initial_clean_build",
            temp_ws,
            prep_clean,
            build_args=[],
            expected_executed_units=6,
            expected_dirty_units=0,
            expected_fresh_units=0,
            expected_new_units=6,
        )

        # 2. No-change rebuild
        def prep_nochange(ws: Path):
            pass

        res_nochange = run_scenario(
            "2_no_change_rebuild",
            temp_ws,
            prep_nochange,
            build_args=[],
            expected_executed_units=0,
            expected_dirty_units=0,
            expected_fresh_units=6,
            expected_new_units=0,
        )

        # 3. Leaf modification (modify leaf_tool/src/main.rs)
        def prep_leaf(ws: Path):
            leaf_src = ws / "leaf_tool" / "src" / "main.rs"
            text = leaf_src.read_text(encoding="utf-8")
            leaf_src.write_text(text + "\n// modified leaf for test\n", encoding="utf-8")

        res_leaf = run_scenario(
            "3_leaf_modification",
            temp_ws,
            prep_leaf,
            build_args=[],
            expected_executed_units=1,
            expected_dirty_units=1,
            expected_fresh_units=5,
            expected_new_units=0,
        )

        # 4. Shared dependency modification (modify string_parser/src/lib.rs)
        def prep_shared(ws: Path):
            parser_src = ws / "string_parser" / "src" / "lib.rs"
            text = parser_src.read_text(encoding="utf-8")
            parser_src.write_text(text + "\n// modified shared dep for test\n", encoding="utf-8")

        res_shared = run_scenario(
            "4_shared_dep_modification",
            temp_ws,
            prep_shared,
            build_args=[],
            expected_executed_units=4,
            expected_dirty_units=4,
            expected_fresh_units=2,
            expected_new_units=0,
        )

        # 5a. Concurrency comparison: -j 1 (serialized)
        def prep_j1(ws: Path):
            run_cmd(["cargo", "clean", "--manifest-path", str(ws / "Cargo.toml")], cwd=ws)

        res_j1 = run_scenario(
            "5a_clean_build_j1",
            temp_ws,
            prep_j1,
            build_args=["-j", "1"],
            expected_executed_units=6,
            expected_dirty_units=0,
            expected_fresh_units=0,
            expected_new_units=6,
        )

        # Assert that -j 1 incurred significant ready delay
        if res_j1["ready_to_start_delays"] < 0.1:
            raise AssertionError(
                f"-j 1 build should have significant ready-to-start queue delays, got {res_j1['ready_to_start_delays']}s"
            )

        # 5b. Concurrency comparison: -j 8 (parallel)
        def prep_j8(ws: Path):
            run_cmd(["cargo", "clean", "--manifest-path", str(ws / "Cargo.toml")], cwd=ws)

        res_j8 = run_scenario(
            "5b_clean_build_j8",
            temp_ws,
            prep_j8,
            build_args=["-j", "8"],
            expected_executed_units=6,
            expected_dirty_units=0,
            expected_fresh_units=0,
            expected_new_units=6,
        )

    # Save consolidated summary
    all_summaries = [res_clean, res_nochange, res_leaf, res_shared, res_j1, res_j8]
    (OUTPUT_DIR / "all_scenarios_summary.json").write_text(
        json.dumps(all_summaries, indent=2), encoding="utf-8"
    )

    print(f"\n{'=' * 70}")
    print("ALL 6 VALIDATION SCENARIOS PASSED WITH INDEPENDENT ASSERTIONS")
    print(f"Outputs written to: {OUTPUT_DIR}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
