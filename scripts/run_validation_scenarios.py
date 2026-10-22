#!/usr/bin/env python3
"""Automated validation harness for Cargo -Zbuild-analysis critical-path prototype.

Executes scenarios against test_workspace:
1. Initial clean build
2. No-change rebuild
3. Leaf modification
4. Shared dependency modification
5. Different job counts (-j 1 vs -j 8)

Validates agreement with Cargo's existing timing report and writes detailed results.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build_trace import BuildAnalysisParser, CriticalPathAnalyzer, format_text_report

WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "test_workspace"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "validation_output"
CARGO_LOG_DIR = Path.home() / ".cargo" / "log"


def run_cmd(cmd: List[str], cwd: Path, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env["RUSTC_BOOTSTRAP"] = "1"
    full_env["CARGO_BUILD_ANALYSIS_ENABLED"] = "true"
    if env:
        full_env.update(env)

    # Use rtk wrapper if available, or direct command
    res = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Command failed ({' '.join(cmd)}):", file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed with code {res.returncode}")
    return res


def get_latest_log_file(before_time: float) -> Path:
    if not CARGO_LOG_DIR.exists():
        raise FileNotFoundError(f"Cargo log dir {CARGO_LOG_DIR} does not exist")

    candidates = [
        p for p in CARGO_LOG_DIR.glob("*.jsonl")
        if p.stat().st_mtime >= before_time - 1.0
    ]
    if not candidates:
        all_logs = sorted(CARGO_LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if all_logs:
            return all_logs[0]
        raise FileNotFoundError(f"No log files found in {CARGO_LOG_DIR}")

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def extract_cargo_timing_unit_data(html_path: Path) -> List[Dict[str, Any]]:
    """Extract UNIT_DATA array embedded in cargo-timing-*.html."""
    if not html_path.exists():
        return []

    content = html_path.read_text(encoding="utf-8")
    m = re.search(r"const UNIT_DATA = (\[.*?\]);", content, re.DOTALL)
    if not m:
        return []

    try:
        return json.loads(m.group(1))
    except Exception as e:
        print(f"Failed to parse UNIT_DATA from HTML: {e}")
        return []


def run_scenario(
    name: str,
    prepare_fn,
    build_args: List[str],
) -> Dict[str, Any]:
    print(f"\n==================================================")
    print(f"Running Scenario: {name}")
    print(f"==================================================")

    prepare_fn()

    start_time = time.time()
    cargo_cmd = ["cargo", "build", "--manifest-path", str(WORKSPACE_DIR / "Cargo.toml"), "-Zbuild-analysis"] + build_args
    print(f"Executing: {' '.join(cargo_cmd)}")
    build_res = run_cmd(cargo_cmd, cwd=WORKSPACE_DIR)

    # Find the generated JSONL file
    log_file = get_latest_log_file(start_time)
    print(f"Captured log: {log_file.name} ({log_file.stat().st_size} bytes)")

    # Run cargo report timings
    timings_cmd = ["cargo", "report", "timings", "-Zbuild-analysis", "--manifest-path", str(WORKSPACE_DIR / "Cargo.toml")]
    run_cmd(timings_cmd, cwd=WORKSPACE_DIR)

    run_id = log_file.stem
    cargo_html_path = WORKSPACE_DIR / "target" / "cargo-timings" / f"cargo-timing-{run_id}.html"

    # Copy artifacts to validation_output
    scenario_dir = OUTPUT_DIR / name.lower().replace(" ", "_").replace("-", "_")
    scenario_dir.mkdir(parents=True, exist_ok=True)

    dest_log = scenario_dir / f"{name}_session.jsonl"
    shutil.copy2(log_file, dest_log)

    if cargo_html_path.exists():
        shutil.copy2(cargo_html_path, scenario_dir / f"{name}_cargo_timing.html")

    # Run our analyzer
    parser = BuildAnalysisParser(strict=False)
    parser.parse_file(dest_log)
    analyzer = CriticalPathAnalyzer(parser)
    rebuild_chains = analyzer.trace_rebuild_reasons()
    crit_path = analyzer.compute_critical_path()

    text_report = format_text_report(parser, analyzer, rebuild_chains, crit_path)
    (scenario_dir / f"{name}_report.txt").write_text(text_report, encoding="utf-8")

    # Extract Cargo timing data for comparison
    cargo_timing_units = extract_cargo_timing_unit_data(cargo_html_path)

    # Check timing agreement
    agreement_records = []
    for c_unit in cargo_timing_units:
        idx = c_unit.get("i")
        c_name = c_unit.get("name")
        c_start = c_unit.get("start")
        c_dur = c_unit.get("duration")

        our_unit = parser.units.get(idx)
        if our_unit:
            our_start = round(our_unit.start_elapsed, 2) if our_unit.start_elapsed is not None else None
            our_dur = round(our_unit.duration, 2) if our_unit.duration is not None else None

            start_diff = abs(our_start - c_start) if our_start is not None and c_start is not None else 0.0
            dur_diff = abs(our_dur - c_dur) if our_dur is not None and c_dur is not None else 0.0

            agrees = start_diff <= 0.02 and dur_diff <= 0.02
            agreement_records.append({
                "index": idx,
                "name": c_name,
                "cargo_start": c_start,
                "our_start": our_start,
                "cargo_duration": c_dur,
                "our_duration": our_dur,
                "start_diff": start_diff,
                "duration_diff": dur_diff,
                "agrees": agrees,
            })

    total_cargo_units = len(cargo_timing_units)
    matching_units = sum(1 for r in agreement_records if r["agrees"])
    agreement_pct = (matching_units / total_cargo_units * 100.0) if total_cargo_units > 0 else 100.0

    print(f"Scenario {name} completed:")
    print(f"  Total units: {len(parser.units)}")
    print(f"  Dirty: {sum(1 for u in parser.units.values() if u.status == 'dirty')}")
    print(f"  Fresh: {sum(1 for u in parser.units.values() if u.status == 'fresh')}")
    print(f"  New: {sum(1 for u in parser.units.values() if u.status == 'new')}")
    print(f"  Critical path steps: {len(crit_path.segments)}")
    print(f"  Total path duration: {crit_path.total_observed_path_duration:.3f}s "
          f"(exec: {crit_path.total_execution_duration:.3f}s, gap: {crit_path.total_scheduler_gaps:.3f}s)")
    print(f"  Timing agreement with Cargo report: {agreement_pct:.1f}% ({matching_units}/{total_cargo_units} units)")

    summary = {
        "scenario": name,
        "total_units": len(parser.units),
        "dirty_units": sum(1 for u in parser.units.values() if u.status == "dirty"),
        "fresh_units": sum(1 for u in parser.units.values() if u.status == "fresh"),
        "new_units": sum(1 for u in parser.units.values() if u.status == "new"),
        "critical_path_steps": len(crit_path.segments),
        "critical_path_duration": crit_path.total_observed_path_duration,
        "execution_duration": crit_path.total_execution_duration,
        "scheduler_gaps": crit_path.total_scheduler_gaps,
        "timing_agreement_pct": agreement_pct,
        "agreement_records": agreement_records,
    }

    (scenario_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Initial clean build
    def prep_clean():
        run_cmd(["cargo", "clean", "--manifest-path", str(WORKSPACE_DIR / "Cargo.toml")], cwd=WORKSPACE_DIR)

    res_clean = run_scenario("1_initial_clean_build", prep_clean, [])

    # 2. No-change rebuild
    def prep_no_change():
        pass  # leave artifacts intact

    res_nochange = run_scenario("2_no_change_rebuild", prep_no_change, [])

    # 3. Leaf modification
    def prep_leaf():
        leaf_file = WORKSPACE_DIR / "leaf_tool" / "src" / "main.rs"
        text = leaf_file.read_text(encoding="utf-8")
        # Add comment
        leaf_file.write_text(text + f"\n// modified at {time.time()}\n", encoding="utf-8")

    res_leaf = run_scenario("3_leaf_modification", prep_leaf, [])

    # 4. Shared dependency modification
    def prep_shared():
        # Modify string_parser
        parser_file = WORKSPACE_DIR / "string_parser" / "src" / "lib.rs"
        text = parser_file.read_text(encoding="utf-8")
        parser_file.write_text(text + f"\n// modified at {time.time()}\n", encoding="utf-8")

    res_shared = run_scenario("4_shared_dep_modification", prep_shared, [])

    # 5a. Different job counts: -j 1 (fully serialized)
    def prep_j1():
        run_cmd(["cargo", "clean", "--manifest-path", str(WORKSPACE_DIR / "Cargo.toml")], cwd=WORKSPACE_DIR)

    res_j1 = run_scenario("5a_clean_build_j1", prep_j1, ["-j", "1"])

    # 5b. Different job counts: -j 8 (high parallelism)
    def prep_j8():
        run_cmd(["cargo", "clean", "--manifest-path", str(WORKSPACE_DIR / "Cargo.toml")], cwd=WORKSPACE_DIR)

    res_j8 = run_scenario("5b_clean_build_j8", prep_j8, ["-j", "8"])

    # Consolidate overall validation summary
    all_summaries = [res_clean, res_nochange, res_leaf, res_shared, res_j1, res_j8]
    (OUTPUT_DIR / "all_scenarios_summary.json").write_text(
        json.dumps(all_summaries, indent=2), encoding="utf-8"
    )

    print("\n==================================================")
    print("ALL VALIDATION SCENARIOS COMPLETED SUCCESSFULLY")
    print(f"Results stored in: {OUTPUT_DIR}")
    print("==================================================")


if __name__ == "__main__":
    main()
