#!/usr/bin/env python3
"""Prototype incremental build critical-path analyzer using Cargo's -Zbuild-analysis logs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Set, Tuple


class LogParseError(Exception):
    """Raised when log parsing fails."""

    def __init__(self, message: str, line_no: int, raw_line: str):
        super().__init__(f"Line {line_no}: {message}")
        self.line_no = line_no
        self.raw_line = raw_line


@dataclass
class LogEntry:
    line_no: int
    run_id: str
    timestamp: str
    reason: str
    raw: Dict[str, Any]


@dataclass
class UnblockEvent:
    unblocker_index: int
    kind: str  # "rmeta" | "finish"
    elapsed: float


@dataclass
class UnitRecord:
    index: int
    package_id: str
    name: str
    version: str
    target_name: str
    target_kind: str
    mode: str
    platform: str
    requested: bool
    features: List[str] = field(default_factory=list)
    dependencies: List[int] = field(default_factory=list)  # unit indices this unit depends on
    dependents: List[int] = field(default_factory=list)  # reverse dependencies

    # Fingerprint & rebuild status
    status: Optional[str] = None  # "new", "dirty", "fresh"
    cause: Optional[Dict[str, Any]] = None

    # Timings (seconds elapsed from build start)
    start_elapsed: Optional[float] = None
    rmeta_elapsed: Optional[float] = None
    finish_elapsed: Optional[float] = None

    # Unblocking telemetry recorded in log
    unblocked_by: Optional[UnblockEvent] = None
    unblocked_units: List[int] = field(default_factory=list)  # unblocked on full finish
    unblocked_rmeta_units: List[int] = field(default_factory=list)  # unblocked on rmeta finish

    @property
    def display_name(self) -> str:
        if self.target_kind in ("lib", ""):
            return f"{self.name} v{self.version}"
        return f"{self.name} v{self.version} ({self.target_name}:{self.target_kind})"

    @property
    def duration(self) -> Optional[float]:
        if self.start_elapsed is not None and self.finish_elapsed is not None:
            return max(0.0, self.finish_elapsed - self.start_elapsed)
        return None

    @property
    def rmeta_duration(self) -> Optional[float]:
        if self.start_elapsed is not None and self.rmeta_elapsed is not None:
            return max(0.0, self.rmeta_elapsed - self.start_elapsed)
        return None


@dataclass
class SessionInfo:
    run_id: Optional[str] = None
    command: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    host: Optional[str] = None
    jobs: Optional[int] = None
    num_cpus: Optional[int] = None
    profile: Optional[str] = None
    rustc_version: Optional[str] = None
    rustc_version_verbose: Optional[str] = None
    target_dir: Optional[str] = None
    workspace_root: Optional[str] = None
    build_started_timestamp: Optional[str] = None
    resolution_started: Optional[float] = None
    resolution_finished: Optional[float] = None
    unit_graph_started: Optional[float] = None
    unit_graph_finished: Optional[float] = None


@dataclass
class RebuildChain:
    unit_index: int
    status: str
    root_cause_index: Optional[int]
    root_cause_summary: str
    full_chain: List[int]  # [unit_index, ..., root_cause_index]
    is_ambiguous: bool
    ambiguity_reason: Optional[str] = None
    explicit_cause_kind: Optional[str] = None


@dataclass
class CriticalPathSegment:
    step: int
    unit_index: int
    unit_name: str
    target_kind: str
    phase_unblocked: str  # "rmeta" or "finish" (how this unit unblocked the next unit)
    start_elapsed: float
    effective_end_elapsed: float
    full_finish_elapsed: Optional[float]
    execution_duration: float  # time spent on critical path by this unit
    scheduler_gap_before: float  # gap between unblock event and start
    unblocked_by_index: Optional[int]
    unblocked_by_kind: Optional[str]
    unblock_time: Optional[float]
    is_observed_root: bool
    note: str = ""


@dataclass
class CriticalPathResult:
    terminal_unit_index: int
    segments: List[CriticalPathSegment]
    total_observed_path_duration: float
    total_execution_duration: float
    total_scheduler_gaps: float
    initial_gap: float
    unreconstructed_notes: List[str]


def parse_package_id(package_id_str: str) -> Tuple[str, str]:
    """Extract (name, version) from Cargo PackageIdSpec or URL string."""
    # Examples:
    # "path+file:///path/to/crate#name#0.1.0"
    # "path+file:///path/to/crate#0.1.0"
    # "core-lib 0.1.0 (path+file:///...)"
    # "registry+https://github.com/rust-lang/crates.io-index#serde#1.0.210"
    if not package_id_str:
        return ("unknown", "unknown")

    if "#" in package_id_str:
        parts = package_id_str.split("#")
        if len(parts) >= 3:
            return (parts[1], parts[2])
        if len(parts) == 2:
            left, right = parts[0], parts[1]
            crate_name = left.rstrip("/").split("/")[-1]
            return (crate_name, right)

    tokens = package_id_str.split()
    if len(tokens) >= 2:
        return (tokens[0], tokens[1])
    return (package_id_str, "unknown")


def extract_explicit_dep_index(cause: Optional[Dict[str, Any]]) -> Optional[int]:
    """Extract explicitly referenced dependency unit index from DirtyReason dict."""
    if not isinstance(cause, dict):
        return None

    dirty_reason = cause.get("dirty_reason")
    if dirty_reason == "unit-dependency-info-changed":
        unit = cause.get("unit")
        return int(unit) if unit is not None else None

    if dirty_reason == "fs-status-outdated":
        fs_status = cause.get("fs_status")
        if fs_status in ("stale-dependency", "stale-dep-fingerprint"):
            unit = cause.get("unit")
            return int(unit) if unit is not None else None

    # Check direct unit fields if present in nested structures
    if "unit" in cause and isinstance(cause["unit"], (int, float)):
        return int(cause["unit"])

    return None


def describe_dirty_reason(cause: Optional[Dict[str, Any]], status: Optional[str]) -> str:
    """Produce human-readable description for dirty reason."""
    if status == "new":
        return "fresh build (no previous artifact)"
    if status == "fresh":
        return "up-to-date (cached)"
    if not cause or not isinstance(cause, dict):
        return "dirty (unspecified reason)"

    reason = cause.get("dirty_reason", "unknown")
    if reason == "unit-dependency-info-changed":
        dep = cause.get("unit")
        return f"dependency info changed (unit {dep})"
    if reason == "fs-status-outdated":
        fs_status = cause.get("fs_status", "stale")
        if fs_status == "stale-dependency":
            dep = cause.get("unit")
            return f"dependency artifact stale (unit {dep})"
        if fs_status == "stale-dep-fingerprint":
            dep = cause.get("unit")
            return f"dependency fingerprint stale (unit {dep})"
        if fs_status == "stale-item":
            item = cause.get("stale_item") or cause.get("item") or cause
            if isinstance(item, dict):
                stale_path = item.get("stale") or item.get("path") or item.get("source")
                if stale_path:
                    return f"file modified: {stale_path}"
                if "var" in item:
                    return f"environment variable changed: {item.get('var')}"
            return "filesystem input modified"
        return f"filesystem stale ({fs_status})"
    if reason == "fresh-build":
        return "fresh build"
    if reason == "features-changed":
        return f"features changed: {cause.get('old')} -> {cause.get('new')}"
    if reason == "declared-features-changed":
        return f"declared features changed: {cause.get('old')} -> {cause.get('new')}"
    if reason == "rustflags-changed":
        return "rustflags changed"
    if reason == "rustc-changed":
        return "compiler toolchain changed"
    if reason == "env-var-changed":
        return f"env var '{cause.get('name')}' changed"
    if reason == "env-vars-changed":
        return "env vars changed"
    if reason == "nothing-obvious":
        return "nothing obvious (unexplained dirty status)"
    if reason == "forced":
        return "forced rebuild"
    return f"{reason}"


class BuildAnalysisParser:
    """Parses Cargo -Zbuild-analysis JSONL files into structured session models."""

    def __init__(self, strict: bool = False):
        self.strict = strict
        self.session = SessionInfo()
        self.units: Dict[int, UnitRecord] = {}
        self.parse_warnings: List[str] = []
        self.malformed_lines: List[Tuple[int, str, str]] = []
        self.unrecognized_events: List[str] = []
        self.total_lines = 0

    def parse_file(self, file_path: Path | str) -> BuildAnalysisParser:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Log file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                self.parse_line(line_no, line)

        self._post_process()
        return self

    def parse_str(self, content: str) -> BuildAnalysisParser:
        for line_no, line in enumerate(content.splitlines(), start=1):
            self.parse_line(line_no, line)
        self._post_process()
        return self

    def parse_line(self, line_no: int, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return

        self.total_lines += 1
        try:
            raw_entry = json.loads(line)
        except json.JSONDecodeError as e:
            msg = f"Invalid JSON syntax: {e}"
            self.malformed_lines.append((line_no, raw_line, msg))
            if self.strict:
                raise LogParseError(msg, line_no, raw_line) from e
            return

        if not isinstance(raw_entry, dict):
            msg = "Expected JSON object top-level"
            self.malformed_lines.append((line_no, raw_line, msg))
            if self.strict:
                raise LogParseError(msg, line_no, raw_line)
            return

        run_id = raw_entry.get("run_id")
        timestamp = raw_entry.get("timestamp")
        reason = raw_entry.get("reason")

        if not reason or not isinstance(reason, str):
            msg = "Missing or invalid 'reason' discriminator in event"
            self.malformed_lines.append((line_no, raw_line, msg))
            if self.strict:
                raise LogParseError(msg, line_no, raw_line)
            return

        if self.session.run_id is None and run_id:
            self.session.run_id = str(run_id)

        try:
            self._handle_event(reason, raw_entry)
        except (KeyError, TypeError, ValueError) as e:
            msg = f"Malformed payload for reason '{reason}': {e}"
            self.malformed_lines.append((line_no, raw_line, msg))
            if self.strict:
                raise LogParseError(msg, line_no, raw_line) from e

    def _handle_event(self, reason: str, data: Dict[str, Any]) -> None:
        if reason == "build-started":
            self.session.build_started_timestamp = data.get("timestamp")
            self.session.command = list(data.get("command", []))
            self.session.cwd = data.get("cwd")
            self.session.host = data.get("host")
            self.session.jobs = data.get("jobs")
            self.session.num_cpus = data.get("num_cpus")
            self.session.profile = data.get("profile")
            self.session.rustc_version = data.get("rustc_version")
            self.session.rustc_version_verbose = data.get("rustc_version_verbose")
            self.session.target_dir = data.get("target_dir")
            self.session.workspace_root = data.get("workspace_root")

        elif reason == "resolution-started":
            self.session.resolution_started = float(data.get("elapsed", 0.0))

        elif reason == "resolution-finished":
            self.session.resolution_finished = float(data.get("elapsed", 0.0))

        elif reason == "unit-graph-started":
            self.session.unit_graph_started = float(data.get("elapsed", 0.0))

        elif reason == "unit-graph-finished":
            self.session.unit_graph_finished = float(data.get("elapsed", 0.0))

        elif reason == "unit-registered":
            index = int(data["index"])
            pkg_id_str = str(data.get("package_id", ""))
            name, version = parse_package_id(pkg_id_str)
            target = data.get("target") or {}
            target_name = target.get("name", name)
            target_kind = target.get("kind", "lib")
            mode = str(data.get("mode", "build"))
            platform = str(data.get("platform", ""))
            requested = bool(data.get("requested", False))
            features = list(data.get("features", []))
            deps = [int(x) for x in data.get("dependencies", [])]

            unit = UnitRecord(
                index=index,
                package_id=pkg_id_str,
                name=name,
                version=version,
                target_name=target_name,
                target_kind=target_kind,
                mode=mode,
                platform=platform,
                requested=requested,
                features=features,
                dependencies=deps,
            )
            self.units[index] = unit

        elif reason == "unit-fingerprint":
            index = int(data["index"])
            unit = self._ensure_unit(index)
            status = data.get("status")
            unit.status = str(status) if status else None
            unit.cause = data.get("cause")

        elif reason == "unit-started":
            index = int(data["index"])
            unit = self._ensure_unit(index)
            unit.start_elapsed = float(data["elapsed"])

        elif reason == "unit-rmeta-finished":
            index = int(data["index"])
            unit = self._ensure_unit(index)
            elapsed = float(data["elapsed"])
            unit.rmeta_elapsed = elapsed
            unblocked = [int(x) for x in data.get("unblocked", [])]
            unit.unblocked_rmeta_units = unblocked

            for target_idx in unblocked:
                target_unit = self._ensure_unit(target_idx)
                # Overwrite or record first unblocker
                target_unit.unblocked_by = UnblockEvent(
                    unblocker_index=index,
                    kind="rmeta",
                    elapsed=elapsed,
                )

        elif reason == "unit-finished":
            index = int(data["index"])
            unit = self._ensure_unit(index)
            elapsed = float(data["elapsed"])
            unit.finish_elapsed = elapsed
            unblocked = [int(x) for x in data.get("unblocked", [])]
            unit.unblocked_units = unblocked

            for target_idx in unblocked:
                target_unit = self._ensure_unit(target_idx)
                # If target was already unblocked by rmeta, rmeta unblocked it earlier
                if target_unit.unblocked_by is None:
                    target_unit.unblocked_by = UnblockEvent(
                        unblocker_index=index,
                        kind="finish",
                        elapsed=elapsed,
                    )

        elif reason in ("unit-section-started", "unit-section-finished"):
            # Accepted for forward compatibility with -Zsection-timings
            pass

        else:
            self.unrecognized_events.append(reason)

    def _ensure_unit(self, index: int) -> UnitRecord:
        if index not in self.units:
            # Tolerant of out-of-order events or missing unit-registered
            self.units[index] = UnitRecord(
                index=index,
                package_id=f"unit-{index}",
                name=f"unit-{index}",
                version="unknown",
                target_name=f"unit-{index}",
                target_kind="unknown",
                mode="unknown",
                platform="unknown",
                requested=False,
            )
            self.parse_warnings.append(f"Event referenced unit {index} before 'unit-registered'")
        return self.units[index]

    def _post_process(self) -> None:
        # Build reverse dependencies (dependents)
        for unit in self.units.values():
            for dep_idx in unit.dependencies:
                dep_unit = self.units.get(dep_idx)
                if dep_unit and unit.index not in dep_unit.dependents:
                    dep_unit.dependents.append(unit.index)


class CriticalPathAnalyzer:
    """Performs critical-path reconstruction, rebuild-reason tracing, and gap attribution."""

    def __init__(self, parser: BuildAnalysisParser):
        self.parser = parser
        self.units = parser.units
        self.session = parser.session

    def trace_rebuild_reasons(self) -> Dict[int, RebuildChain]:
        """Traces rebuild causes recursively using explicit dependency pointers.

        Does not assume dirty dependencies are the cause unless explicitly linked.
        """
        results: Dict[int, RebuildChain] = {}

        for index, unit in sorted(self.units.items()):
            if unit.status not in ("dirty", "new"):
                continue

            chain, is_ambiguous, ambiguity_reason, root_idx = self._trace_chain_for_unit(unit)
            root_unit = self.units.get(root_idx) if root_idx is not None else None
            root_cause_summary = describe_dirty_reason(
                root_unit.cause if root_unit else unit.cause,
                root_unit.status if root_unit else unit.status,
            )

            results[index] = RebuildChain(
                unit_index=index,
                status=unit.status or "unknown",
                root_cause_index=root_idx,
                root_cause_summary=root_cause_summary,
                full_chain=chain,
                is_ambiguous=is_ambiguous,
                ambiguity_reason=ambiguity_reason,
                explicit_cause_kind=(
                    unit.cause.get("dirty_reason") if isinstance(unit.cause, dict) else None
                ),
            )

        return results

    def _trace_chain_for_unit(
        self, unit: UnitRecord
    ) -> Tuple[List[int], bool, Optional[str], Optional[int]]:
        """Follow explicit dirty-reason dependency pointers recursively."""
        visited: Set[int] = set()
        current: Optional[UnitRecord] = unit
        chain: List[int] = []

        while current is not None:
            chain.append(current.index)
            visited.add(current.index)

            explicit_dep_idx = extract_explicit_dep_index(current.cause)
            if explicit_dep_idx is None:
                # Leaf of explicit chain.
                # Check whether there is an ambiguity:
                # If current unit didn't report an explicit dependency cause,
                # but it has dependencies that were rebuilt:
                dirty_deps = [
                    d
                    for d in current.dependencies
                    if self.units.get(d) and self.units[d].status in ("dirty", "new")
                ]

                is_ambiguous = False
                ambiguity_reason = None

                # If status is dirty but reason is generic/missing and has dirty dependencies
                cause_kind = (
                    current.cause.get("dirty_reason") if isinstance(current.cause, dict) else None
                )
                if (
                    cause_kind in (None, "nothing-obvious", "stale", "forced")
                    and dirty_deps
                    and current.index == unit.index
                ):
                    is_ambiguous = True
                    dep_names = [self.units[d].display_name for d in dirty_deps]
                    ambiguity_reason = (
                        f"Unit is dirty with non-specific cause ('{cause_kind}'), "
                        f"though upstream dependencies rebuilt: {', '.join(dep_names)}"
                    )

                return (chain, is_ambiguous, ambiguity_reason, current.index)

            if explicit_dep_idx in visited:
                # Cycle detected
                return (
                    chain,
                    True,
                    f"Cycle detected in explicit dirty-reason chain at unit {explicit_dep_idx}",
                    current.index,
                )

            next_unit = self.units.get(explicit_dep_idx)
            if next_unit is None:
                return (
                    chain,
                    True,
                    f"Explicit dirty-reason referenced unknown unit {explicit_dep_idx}",
                    current.index,
                )

            current = next_unit

        return (chain, False, None, unit.index)

    def compute_critical_path(
        self, target_unit_index: Optional[int] = None
    ) -> CriticalPathResult:
        """Reconstructs the observed execution critical path using recorded unblocking events.

        Honors metadata pipelining (rmeta unblocking) and separates unit execution
        durations from scheduler/queue gaps.
        """
        started_units = [u for u in self.units.values() if u.start_elapsed is not None]
        unreconstructed_notes: List[str] = []

        if not started_units:
            return CriticalPathResult(
                terminal_unit_index=-1,
                segments=[],
                total_observed_path_duration=0.0,
                total_execution_duration=0.0,
                total_scheduler_gaps=0.0,
                initial_gap=0.0,
                unreconstructed_notes=["No units recorded start events in this log session."],
            )

        # 1. Determine terminal unit
        if target_unit_index is not None:
            if target_unit_index not in self.units:
                raise ValueError(f"Requested target unit {target_unit_index} does not exist")
            terminal_unit = self.units[target_unit_index]
            if terminal_unit.finish_elapsed is None:
                unreconstructed_notes.append(
                    f"Target unit {target_unit_index} did not complete execution in log."
                )
        else:
            # Pick unit that finished latest among started units
            finished_units = [u for u in started_units if u.finish_elapsed is not None]
            if finished_units:
                terminal_unit = max(finished_units, key=lambda u: u.finish_elapsed or 0.0)
            else:
                terminal_unit = max(started_units, key=lambda u: u.start_elapsed or 0.0)

        # 2. Backward walk along unblock events
        segments_rev: List[CriticalPathSegment] = []
        visited_units: Set[int] = set()

        curr_unit: Optional[UnitRecord] = terminal_unit
        # For the terminal unit, its phase required on critical path is full completion
        phase_required = "finish"

        queue_start = self.session.unit_graph_finished or 0.0

        while curr_unit is not None:
            visited_units.add(curr_unit.index)

            start = curr_unit.start_elapsed or 0.0
            full_finish = curr_unit.finish_elapsed

            if phase_required == "rmeta" and curr_unit.rmeta_elapsed is not None:
                effective_end = curr_unit.rmeta_elapsed
            else:
                effective_end = curr_unit.finish_elapsed or (
                    curr_unit.rmeta_elapsed if curr_unit.rmeta_elapsed is not None else start
                )

            execution_dur = max(0.0, effective_end - start)

            unblock_ev = curr_unit.unblocked_by
            if unblock_ev is not None:
                scheduler_gap = max(0.0, start - unblock_ev.elapsed)
                unblock_idx = unblock_ev.unblocker_index
                unblock_kind = unblock_ev.kind
                unblock_time = unblock_ev.elapsed
                is_root = False
                note = ""
            else:
                # No recorded unblocker
                is_root = True
                unblock_idx = None
                unblock_kind = None
                unblock_time = None
                scheduler_gap = max(0.0, start - queue_start)

                if not curr_unit.dependencies:
                    note = "DAG root (no dependencies in unit graph)"
                else:
                    # Unit had dependencies, but no unblocking event was logged.
                    # This happens when all dependencies were fresh (cached), because
                    # Cargo omits UnitFinished/UnitRmetaFinished for fresh units.
                    fresh_deps = [
                        d for d in curr_unit.dependencies if self.units.get(d, None) and self.units[d].status == "fresh"
                    ]
                    if len(fresh_deps) == len(curr_unit.dependencies):
                        note = "Unblocked by fresh dependencies (Cargo does not log unblock events for fresh units)"
                        unreconstructed_notes.append(
                            f"Unit {curr_unit.display_name} (index {curr_unit.index}) was unblocked by "
                            f"fresh dependency units {[self.units[d].name for d in fresh_deps]}, but Cargo's "
                            f"log does not emit unblock events for fresh units."
                        )
                    else:
                        note = "Missing unblocking event in log (unrecorded unblock predecessor)"
                        unreconstructed_notes.append(
                            f"Unit {curr_unit.display_name} (index {curr_unit.index}) had dirty/untracked "
                            f"dependencies but no recorded unblocking event."
                        )

            seg = CriticalPathSegment(
                step=0,  # will assign in forward order
                unit_index=curr_unit.index,
                unit_name=curr_unit.display_name,
                target_kind=curr_unit.target_kind,
                phase_unblocked=phase_required,
                start_elapsed=start,
                effective_end_elapsed=effective_end,
                full_finish_elapsed=full_finish,
                execution_duration=execution_dur,
                scheduler_gap_before=scheduler_gap,
                unblocked_by_index=unblock_idx,
                unblocked_by_kind=unblock_kind,
                unblock_time=unblock_time,
                is_observed_root=is_root,
                note=note,
            )
            segments_rev.append(seg)

            if unblock_ev is None:
                # Reached root of observed unblocking chain
                break

            prev_idx = unblock_ev.unblocker_index
            if prev_idx in visited_units:
                unreconstructed_notes.append(
                    f"Cycle encountered in unblocking chain at unit index {prev_idx}"
                )
                break

            prev_unit = self.units.get(prev_idx)
            if prev_unit is None:
                unreconstructed_notes.append(
                    f"Unblock event referenced non-existent unit index {prev_idx}"
                )
                break

            # The phase of the previous unit that unblocked this unit is recorded in unblock_ev.kind
            phase_required = unblock_ev.kind
            curr_unit = prev_unit

        # Reverse to get forward chronological order
        segments = list(reversed(segments_rev))
        for i, s in enumerate(segments, start=1):
            s.step = i

        total_exec = sum(s.execution_duration for s in segments)
        total_gaps = sum(s.scheduler_gap_before for s in segments)
        initial_gap = segments[0].scheduler_gap_before if segments else 0.0

        # Total observed span from start of first unblocker or root to terminal finish
        if segments:
            first_start = segments[0].start_elapsed
            last_end = segments[-1].effective_end_elapsed
            total_duration = max(0.0, last_end - first_start + initial_gap)
        else:
            total_duration = 0.0

        return CriticalPathResult(
            terminal_unit_index=terminal_unit.index,
            segments=segments,
            total_observed_path_duration=total_duration,
            total_execution_duration=total_exec,
            total_scheduler_gaps=total_gaps,
            initial_gap=initial_gap,
            unreconstructed_notes=unreconstructed_notes,
        )


def format_text_report(
    parser: BuildAnalysisParser,
    analyzer: CriticalPathAnalyzer,
    rebuild_chains: Dict[int, RebuildChain],
    crit_path: CriticalPathResult,
) -> str:
    """Renders formatted plain-text analysis report."""
    sess = parser.session
    units = parser.units

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("CARGO INCREMENTAL BUILD CRITICAL-PATH ANALYSIS REPORT")
    lines.append("=" * 80)
    lines.append("")

    # 1. Session Information
    lines.append("## 1. SESSION INFORMATION")
    lines.append(f"  Run ID:              {sess.run_id or 'N/A'}")
    lines.append(f"  Timestamp:           {sess.build_started_timestamp or 'N/A'}")
    lines.append(f"  Command:             {' '.join(sess.command) if sess.command else 'N/A'}")
    lines.append(f"  Working Directory:   {sess.cwd or 'N/A'}")
    lines.append(f"  Workspace Root:      {sess.workspace_root or 'N/A'}")
    lines.append(f"  Target Directory:    {sess.target_dir or 'N/A'}")
    lines.append(f"  Host Platform:       {sess.host or 'N/A'}")
    lines.append(f"  Profile:             {sess.profile or 'N/A'}")
    lines.append(f"  Jobs (-j):           {sess.jobs if sess.jobs is not None else 'N/A'}")
    lines.append(f"  Available CPUs:      {sess.num_cpus if sess.num_cpus is not None else 'N/A'}")
    lines.append(f"  Rustc Version:       {sess.rustc_version or 'N/A'}")
    if sess.unit_graph_finished is not None:
        lines.append(f"  Unit Graph Time:     {sess.unit_graph_finished:.4f}s")
    lines.append("")

    # 2. Units Summary
    fresh_count = sum(1 for u in units.values() if u.status == "fresh")
    dirty_count = sum(1 for u in units.values() if u.status == "dirty")
    new_count = sum(1 for u in units.values() if u.status == "new")
    unknown_count = sum(1 for u in units.values() if u.status not in ("fresh", "dirty", "new"))
    started_count = sum(1 for u in units.values() if u.start_elapsed is not None)

    lines.append("## 2. UNITS SUMMARY")
    lines.append(f"  Total Registered Units: {len(units)}")
    lines.append(f"  - Fresh (Cached):       {fresh_count}")
    lines.append(f"  - Dirty (Rebuilding):   {dirty_count}")
    lines.append(f"  - New (First Build):    {new_count}")
    if unknown_count:
        lines.append(f"  - Unknown Status:       {unknown_count}")
    lines.append(f"  - Units Executed:       {started_count}")
    lines.append("")

    lines.append("  Detailed Unit Status Table:")
    lines.append(
        f"  {'Idx':<5} {'Unit Name':<28} {'Kind':<10} {'Status':<8} "
        f"{'Start (s)':<11} {'Rmeta (s)':<11} {'Finish (s)':<11} {'Duration':<10}"
    )
    lines.append("  " + "-" * 100)
    for idx, u in sorted(units.items()):
        start_str = f"{u.start_elapsed:.3f}" if u.start_elapsed is not None else "-"
        rmeta_str = f"{u.rmeta_elapsed:.3f}" if u.rmeta_elapsed is not None else "-"
        finish_str = f"{u.finish_elapsed:.3f}" if u.finish_elapsed is not None else "-"
        dur_str = f"{u.duration:.3f}s" if u.duration is not None else "-"
        lines.append(
            f"  {u.index:<5} {u.display_name[:26]:<28} {u.target_kind:<10} {u.status or 'N/A':<8} "
            f"{start_str:<11} {rmeta_str:<11} {finish_str:<11} {dur_str:<10}"
        )
    lines.append("")

    # 3. Rebuild Reason Chains
    lines.append("## 3. REBUILD REASON ATTRIBUTION CHAINS")
    if not rebuild_chains:
        lines.append("  No rebuilt units (all units fresh / up-to-date).")
    else:
        for idx, chain in sorted(rebuild_chains.items()):
            unit = units[idx]
            root_desc = chain.root_cause_summary
            chain_str = " -> ".join(f"unit {i} ({units[i].name})" for i in chain.full_chain)

            flag = " [AMBIGUOUS]" if chain.is_ambiguous else ""
            lines.append(f"  * Unit {idx} [{unit.display_name}] status={chain.status}{flag}:")
            lines.append(f"      Causal Chain: {chain_str}")
            lines.append(f"      Root Invalidation Cause: {root_desc}")
            if chain.is_ambiguous and chain.ambiguity_reason:
                lines.append(f"      Ambiguity Note: {chain.ambiguity_reason}")
    lines.append("")

    # 4. Observed Critical Path
    lines.append("## 4. OBSERVED EXECUTION CRITICAL PATH")
    if not crit_path.segments:
        lines.append("  No critical path reconstructed.")
    else:
        term = units.get(crit_path.terminal_unit_index)
        term_desc = term.display_name if term else f"unit {crit_path.terminal_unit_index}"
        lines.append(f"  Terminal Target Unit: {term_desc} (index {crit_path.terminal_unit_index})")
        lines.append("")
        lines.append(
            f"  {'Step':<5} {'Unit Name':<28} {'Phase':<8} {'Start (s)':<10} "
            f"{'End (s)':<10} {'Exec Time':<11} {'Wait Gap':<10} {'Unblocked By'}"
        )
        lines.append("  " + "-" * 105)

        for s in crit_path.segments:
            unblock_str = (
                f"unit {s.unblocked_by_index} ({s.unblocked_by_kind} @ {s.unblock_time:.3f}s)"
                if s.unblocked_by_index is not None
                else "DAG root / initial"
            )
            lines.append(
                f"  {s.step:<5} {s.unit_name[:26]:<28} {s.phase_unblocked:<8} "
                f"{s.start_elapsed:<10.3f} {s.effective_end_elapsed:<10.3f} "
                f"{s.execution_duration:<10.3f}s {s.scheduler_gap_before:<9.3f}s {unblock_str}"
            )
            if s.note:
                lines.append(f"        Note: {s.note}")

    lines.append("")

    # 5. Timing Breakdown
    lines.append("## 5. TIMING AND CRITICAL-PATH BUDGET")
    lines.append(f"  Total Observed Critical Path Duration: {crit_path.total_observed_path_duration:.4f}s")
    lines.append(f"  - Active Unit Execution on Path:        {crit_path.total_execution_duration:.4f}s "
                 f"({crit_path.total_execution_duration / max(0.0001, crit_path.total_observed_path_duration) * 100:.1f}%)")
    lines.append(f"  - Unexplained Scheduler Gaps on Path:   {crit_path.total_scheduler_gaps:.4f}s "
                 f"({crit_path.total_scheduler_gaps / max(0.0001, crit_path.total_observed_path_duration) * 100:.1f}%)")
    lines.append(f"    (Initial Queue Delay before 1st unit: {crit_path.initial_gap:.4f}s)")
    lines.append("")

    # 6. Unreconstructed / Missing Information
    lines.append("## 6. EVENTS & INFORMATION THAT COULD NOT BE RECONSTRUCTED")
    all_unreconstructed: List[str] = []

    if parser.malformed_lines:
        all_unreconstructed.append(
            f"Parser encountered {len(parser.malformed_lines)} malformed/unparseable log lines."
        )
    if parser.unrecognized_events:
        all_unreconstructed.append(
            f"Unrecognized event reasons ignored: {list(set(parser.unrecognized_events))}"
        )
    if crit_path.unreconstructed_notes:
        all_unreconstructed.extend(crit_path.unreconstructed_notes)

    # Check for unlogged unblockers on dirty units
    for u in units.values():
        if u.start_elapsed is not None and u.dependencies and u.unblocked_by is None:
            all_unreconstructed.append(
                f"Unit {u.display_name} (index {u.index}) started at {u.start_elapsed:.3f}s but has "
                f"no unblocking event in the log. Upstream dependencies were omitted from unblocking telemetry."
            )

    # Check for fresh units lacking timestamps
    if fresh_count > 0:
        all_unreconstructed.append(
            f"{fresh_count} units were fresh (cached). Cargo emits no start or finish events for fresh units, "
            f"making their fingerprint verification time unmeasurable from log events alone."
        )

    # Check for scheduler state
    all_unreconstructed.append(
        "Scheduler queue state (jobserver tokens, running jobs count, OS process fork latency) "
        "is not logged by Cargo, meaning scheduler gaps cannot be definitively assigned to job token starvation vs OS delay."
    )

    if not all_unreconstructed:
        lines.append("  None. All events and relationships were fully reconstructed.")
    else:
        for item in sorted(set(all_unreconstructed)):
            lines.append(f"  * {item}")

    lines.append("")
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Analyze Cargo -Zbuild-analysis logs for critical path and rebuild attribution."
    )
    arg_parser.add_argument("log_file", help="Path to Cargo JSONL build analysis log file (or '-' for stdin)")
    arg_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output report format (default: text)"
    )
    arg_parser.add_argument(
        "--target", type=int, default=None, help="Specific UnitIndex to trace critical path for (default: terminal)"
    )
    arg_parser.add_argument(
        "--strict", action="store_true", help="Fail immediately on malformed JSONL lines"
    )

    args = arg_parser.parse_args()

    parser = BuildAnalysisParser(strict=args.strict)
    if args.log_file == "-":
        parser.parse_str(sys.stdin.read())
    else:
        parser.parse_file(args.log_file)

    analyzer = CriticalPathAnalyzer(parser)
    rebuild_chains = analyzer.trace_rebuild_reasons()
    crit_path = analyzer.compute_critical_path(target_unit_index=args.target)

    if args.format == "json":
        data = {
            "session": parser.session.__dict__,
            "units": {k: v.__dict__ for k, v in parser.units.items()},
            "rebuild_chains": {
                k: {
                    "unit_index": v.unit_index,
                    "status": v.status,
                    "root_cause_index": v.root_cause_index,
                    "root_cause_summary": v.root_cause_summary,
                    "full_chain": v.full_chain,
                    "is_ambiguous": v.is_ambiguous,
                    "ambiguity_reason": v.ambiguity_reason,
                }
                for k, v in rebuild_chains.items()
            },
            "critical_path": {
                "terminal_unit_index": crit_path.terminal_unit_index,
                "segments": [s.__dict__ for s in crit_path.segments],
                "total_observed_path_duration": crit_path.total_observed_path_duration,
                "total_execution_duration": crit_path.total_execution_duration,
                "total_scheduler_gaps": crit_path.total_scheduler_gaps,
                "initial_gap": crit_path.initial_gap,
                "unreconstructed_notes": crit_path.unreconstructed_notes,
            },
        }
        print(json.dumps(data, indent=2, default=str))
    else:
        report = format_text_report(parser, analyzer, rebuild_chains, crit_path)
        print(report)


if __name__ == "__main__":
    main()
