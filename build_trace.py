#!/usr/bin/env python3
"""Incremental build critical-path and rebuild-attribution analyzer for Cargo -Zbuild-analysis logs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
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
class UnblockEvent:
    unblocker_index: int
    kind: str  # "rmeta" | "finish"
    elapsed: float


@dataclass
class EffectiveSchedulingDep:
    dep_index: int
    required_artifact: str  # "metadata" | "all"
    is_synthetic: bool      # True if added transitively for linkable targets


@dataclass
class ParsedDirtyReason:
    kind: str
    summary: str
    explicit_dep_unit: Optional[int] = None
    stale_path: Optional[str] = None
    reference_path: Optional[str] = None
    stale_item: Optional[str] = None
    stale_mtime: Optional[float] = None
    reference_mtime: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)


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

    # 1. Registered dependency graph (emitted in UnitRegistered)
    dependencies: List[int] = field(default_factory=list)
    dependents: List[int] = field(default_factory=list)

    # 2. Inferred effective scheduling graph (metadata vs all, direct vs synthetic)
    effective_dependencies: List[EffectiveSchedulingDep] = field(default_factory=list)

    # 3. Fingerprint & rebuild causality
    status: Optional[str] = None  # "new", "dirty", "fresh"
    cause_raw: Optional[Dict[str, Any]] = None
    parsed_cause: Optional[ParsedDirtyReason] = None

    # Timings (seconds elapsed from build start)
    start_elapsed: Optional[float] = None
    rmeta_elapsed: Optional[float] = None
    finish_elapsed: Optional[float] = None

    # Observed unblocking telemetry
    unblocked_by: Optional[UnblockEvent] = None
    unblocked_units: List[int] = field(default_factory=list)        # on full finish
    unblocked_rmeta_units: List[int] = field(default_factory=list)  # on rmeta finish

    @property
    def display_name(self) -> str:
        if self.target_kind in ("lib", ""):
            return f"{self.name} v{self.version}"
        return f"{self.name} v{self.version} ({self.target_name}:{self.target_kind})"

    @property
    def requires_upstream_objects(self) -> bool:
        """Mirrors Cargo's TargetKind::requires_upstream_objects / CrateType::requires_upstream_objects.

        True for binaries, integration tests, benchmarks, cdylib, etc. that require
        upstream object code (rlib/all) to link rather than just rmeta.
        """
        return self.target_kind not in ("lib", "rlib") or self.mode != "build"

    @property
    def duration(self) -> Optional[float]:
        if self.start_elapsed is not None and self.finish_elapsed is not None:
            return self.finish_elapsed - self.start_elapsed
        return None

    @property
    def rmeta_duration(self) -> Optional[float]:
        if self.start_elapsed is not None and self.rmeta_elapsed is not None:
            return self.rmeta_elapsed - self.start_elapsed
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
    chain_category: str  # "confirmed_explicit" | "local_invalidation" | "ambiguous" | "fresh_build" | "broken_reference"
    root_cause_index: Optional[int]
    root_cause_summary: str
    full_chain: List[int]  # [unit_index, ..., root_cause_index]
    is_ambiguous: bool
    ambiguity_reason: Optional[str] = None
    stale_path: Optional[str] = None


@dataclass
class UnblockingChainSegment:
    step: int
    unit_index: int
    unit_name: str
    target_kind: str
    phase_unblocked: str  # "rmeta" | "finish"
    start_elapsed: float
    effective_end_elapsed: float
    full_finish_elapsed: Optional[float]
    execution_duration: float
    ready_to_start_delay: float
    unblocked_by_index: Optional[int]
    unblocked_by_kind: Optional[str]
    unblock_time: Optional[float]
    is_observed_root: bool
    is_registered_dep_of_successor: bool
    timestamp_inconsistent: bool = False
    note: str = ""


@dataclass
class UnblockingChainResult:
    target_unit_index: int
    target_selection_mode: str  # "explicit_flag" | "requested_target" | "latest_finish"
    segments: List[UnblockingChainSegment]
    total_chain_duration: float
    total_execution_duration: float
    total_ready_to_start_delays: float
    initial_delay: float
    diagnostics: List[str]
    unreconstructed_notes: List[str]


def parse_package_id(package_id_str: str) -> Tuple[str, str]:
    """Extract (name, version) from Cargo PackageIdSpec or URL string."""
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


def parse_dirty_reason(cause: Optional[Dict[str, Any]], status: Optional[str]) -> ParsedDirtyReason:
    """Accurately parses Cargo's serialized DirtyReason according to its serde schema."""
    if status == "new":
        return ParsedDirtyReason(kind="fresh-build", summary="fresh build (no previous artifact)")
    if status == "fresh":
        return ParsedDirtyReason(kind="up-to-date", summary="up-to-date (cached)")
    if not cause or not isinstance(cause, dict):
        return ParsedDirtyReason(kind="unspecified", summary="dirty (unspecified reason)")

    dirty_reason = cause.get("dirty_reason", "unknown")

    # 1. Explicit dependency pointers
    if dirty_reason == "unit-dependency-info-changed":
        unit_raw = cause.get("unit")
        dep_idx = int(unit_raw) if unit_raw is not None else None
        return ParsedDirtyReason(
            kind=dirty_reason,
            summary=f"dependency info changed: unit {dep_idx}",
            explicit_dep_unit=dep_idx,
            details=cause,
        )

    if dirty_reason == "fs-status-outdated":
        fs_status = cause.get("fs_status")
        if fs_status in ("stale-dependency", "stale-dep-fingerprint"):
            unit_raw = cause.get("unit")
            dep_idx = int(unit_raw) if unit_raw is not None else None
            return ParsedDirtyReason(
                kind=f"fs-status-outdated:{fs_status}",
                summary=f"dependency artifact/fingerprint stale: unit {dep_idx}",
                explicit_dep_unit=dep_idx,
                details=cause,
            )

        if fs_status == "stale-item":
            stale_item = cause.get("stale_item")
            stale = cause.get("stale") or cause.get("path") or cause.get("source")
            reference = cause.get("reference")
            stale_mtime = cause.get("stale_mtime")
            ref_mtime = cause.get("reference_mtime")

            summary_item = stale_item or "filesystem input"
            if stale:
                summary = f"{summary_item} modified: {stale}"
            else:
                summary = f"{summary_item} modified"

            return ParsedDirtyReason(
                kind=f"fs-status-outdated:stale-item:{stale_item or 'unknown'}",
                summary=summary,
                stale_path=str(stale) if stale else None,
                reference_path=str(reference) if reference else None,
                stale_item=str(stale_item) if stale_item else None,
                stale_mtime=float(stale_mtime) if stale_mtime is not None else None,
                reference_mtime=float(ref_mtime) if ref_mtime is not None else None,
                details=cause,
            )

        return ParsedDirtyReason(
            kind=f"fs-status-outdated:{fs_status or 'stale'}",
            summary=f"filesystem stale ({fs_status or 'unknown'})",
            details=cause,
        )

    # 2. Local environment and config causes
    if dirty_reason == "fresh-build":
        return ParsedDirtyReason(kind=dirty_reason, summary="fresh build", details=cause)
    if dirty_reason == "features-changed":
        return ParsedDirtyReason(
            kind=dirty_reason,
            summary=f"features changed: {cause.get('old')} -> {cause.get('new')}",
            details=cause,
        )
    if dirty_reason == "declared-features-changed":
        return ParsedDirtyReason(
            kind=dirty_reason,
            summary=f"declared features changed: {cause.get('old')} -> {cause.get('new')}",
            details=cause,
        )
    if dirty_reason == "rustflags-changed":
        return ParsedDirtyReason(kind=dirty_reason, summary="rustflags changed", details=cause)
    if dirty_reason == "rustc-changed":
        return ParsedDirtyReason(kind=dirty_reason, summary="compiler toolchain changed", details=cause)
    if dirty_reason == "env-var-changed":
        return ParsedDirtyReason(
            kind=dirty_reason,
            summary=f"env var '{cause.get('name')}' changed",
            details=cause,
        )
    if dirty_reason == "env-vars-changed":
        return ParsedDirtyReason(kind=dirty_reason, summary="env vars changed", details=cause)
    if dirty_reason == "nothing-obvious":
        return ParsedDirtyReason(kind=dirty_reason, summary="nothing obvious (unexplained dirty status)", details=cause)
    if dirty_reason == "forced":
        return ParsedDirtyReason(kind=dirty_reason, summary="forced rebuild", details=cause)

    return ParsedDirtyReason(kind=dirty_reason, summary=f"{dirty_reason}", details=cause)


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
            unit.cause_raw = data.get("cause")
            unit.parsed_cause = parse_dirty_reason(unit.cause_raw, unit.status)

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
                if target_unit.unblocked_by is None:
                    target_unit.unblocked_by = UnblockEvent(
                        unblocker_index=index,
                        kind="finish",
                        elapsed=elapsed,
                    )

        elif reason in ("unit-section-started", "unit-section-finished"):
            pass

        else:
            self.unrecognized_events.append(reason)

    def _ensure_unit(self, index: int) -> UnitRecord:
        if index not in self.units:
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
        # 1. Reverse direct dependencies
        for unit in self.units.values():
            for dep_idx in unit.dependencies:
                dep_unit = self.units.get(dep_idx)
                if dep_unit and unit.index not in dep_unit.dependents:
                    dep_unit.dependents.append(unit.index)

        # 2. Derive effective scheduling dependencies according to Cargo's JobQueue rules
        for unit in self.units.values():
            effective: List[EffectiveSchedulingDep] = []
            direct_dep_indices = set(unit.dependencies)

            # Direct dependencies
            for dep_idx in unit.dependencies:
                dep_unit = self.units.get(dep_idx)
                # In Cargo: only_requires_rmeta(parent, dep)
                # True if parent is lib and dep is lib and both build mode
                is_rmeta = (
                    dep_unit is not None
                    and not unit.requires_upstream_objects
                    and not dep_unit.requires_upstream_objects
                )
                artifact = "metadata" if is_rmeta else "all"
                effective.append(
                    EffectiveSchedulingDep(
                        dep_index=dep_idx,
                        required_artifact=artifact,
                        is_synthetic=False,
                    )
                )

            # Synthetic transitive dependencies for linkable targets (bin, test, etc.)
            if unit.requires_upstream_objects:
                visited: Set[int] = set()

                def collect_transitive(curr_idx: int) -> None:
                    curr = self.units.get(curr_idx)
                    if not curr:
                        return
                    for d in curr.dependencies:
                        if d not in visited:
                            visited.add(d)
                            if d not in direct_dep_indices:
                                effective.append(
                                    EffectiveSchedulingDep(
                                        dep_index=d,
                                        required_artifact="all",
                                        is_synthetic=True,
                                    )
                                )
                            collect_transitive(d)

                for dep_idx in unit.dependencies:
                    collect_transitive(dep_idx)

            unit.effective_dependencies = effective


class CriticalPathAnalyzer:
    """Performs critical-path reconstruction, rebuild-reason tracing, and gap attribution."""

    def __init__(self, parser: BuildAnalysisParser):
        self.parser = parser
        self.units = parser.units
        self.session = parser.session

    def trace_rebuild_reasons(self) -> Dict[int, RebuildChain]:
        """Traces rebuild causes recursively using explicit dependency pointers.

        Distinguishes confirmed explicit chains, known local invalidations, fresh builds,
        and genuinely ambiguous causes without guessing from the static DAG.
        """
        results: Dict[int, RebuildChain] = {}

        for index, unit in sorted(self.units.items()):
            if unit.status not in ("dirty", "new"):
                continue

            # Fresh builds (clean build or new targets)
            if unit.status == "new":
                results[index] = RebuildChain(
                    unit_index=index,
                    status="new",
                    chain_category="fresh_build",
                    root_cause_index=index,
                    root_cause_summary="fresh build (no previous artifact)",
                    full_chain=[index],
                    is_ambiguous=False,
                    ambiguity_reason=None,
                    stale_path=None,
                )
                continue

            # Dirty units: follow explicit causal pointers
            chain, is_ambiguous, ambiguity_reason, root_idx, category = self._trace_chain_for_unit(unit)
            root_unit = self.units.get(root_idx) if root_idx is not None else None

            if root_unit and root_unit.parsed_cause:
                root_summary = root_unit.parsed_cause.summary
                stale_path = root_unit.parsed_cause.stale_path
            else:
                root_summary = unit.parsed_cause.summary if unit.parsed_cause else "dirty"
                stale_path = unit.parsed_cause.stale_path if unit.parsed_cause else None

            results[index] = RebuildChain(
                unit_index=index,
                status="dirty",
                chain_category=category,
                root_cause_index=root_idx,
                root_cause_summary=root_summary,
                full_chain=chain,
                is_ambiguous=is_ambiguous,
                ambiguity_reason=ambiguity_reason,
                stale_path=stale_path,
            )

        return results

    def _trace_chain_for_unit(
        self, unit: UnitRecord
    ) -> Tuple[List[int], bool, Optional[str], int, str]:
        """Follow explicit dirty-reason dependency pointers recursively."""
        visited: Set[int] = set()
        current: Optional[UnitRecord] = unit
        chain: List[int] = []

        while current is not None:
            chain.append(current.index)
            visited.add(current.index)

            parsed_cause = current.parsed_cause
            dep_ptr = parsed_cause.explicit_dep_unit if parsed_cause else None

            if dep_ptr is None:
                # Leaf of explicit chain
                break

            if dep_ptr in visited:
                # Cycle in explicit cause pointers
                return (
                    chain,
                    True,
                    f"Cycle detected in explicit dirty-reason chain at unit {dep_ptr}",
                    current.index,
                    "broken_reference",
                )

            next_unit = self.units.get(dep_ptr)
            if next_unit is None:
                return (
                    chain,
                    True,
                    f"Explicit dirty-reason referenced unregistered unit index {dep_ptr}",
                    current.index,
                    "broken_reference",
                )

            current = next_unit

        terminal_root = current if current is not None else unit
        root_cause = terminal_root.parsed_cause
        cause_kind = root_cause.kind if root_cause else "unspecified"

        # Check for ambiguity at the chain termination point
        dirty_deps = [
            d for d in terminal_root.dependencies
            if self.units.get(d) and self.units[d].status in ("dirty", "new")
        ]

        if cause_kind in ("unspecified", "nothing-obvious", "stale", "forced"):
            if dirty_deps:
                dep_names = [self.units[d].display_name for d in dirty_deps]
                return (
                    chain,
                    True,
                    f"Causal chain ends at unit {terminal_root.index} ({terminal_root.name}) with non-specific cause "
                    f"'{cause_kind}' while registered dependencies rebuilt: {', '.join(dep_names)}",
                    terminal_root.index,
                    "ambiguous",
                )
            return (chain, False, None, terminal_root.index, "unresolved_local")

        # Explicit chain terminated at a known specific local invalidation cause
        category = "confirmed_explicit" if len(chain) > 1 else "local_invalidation"
        return (chain, False, None, terminal_root.index, category)

    def compute_critical_path(
        self, target_unit_index: Optional[int] = None
    ) -> UnblockingChainResult:
        """Reconstructs the observed dependency-unblocking chain.

        Traces backward along recorded unblocking events from the selected target unit,
        separating unit execution durations from ready-to-start scheduler delays.
        """
        started_units = [u for u in self.units.values() if u.start_elapsed is not None]
        diagnostics: List[str] = []
        unreconstructed_notes: List[str] = []

        if not started_units:
            return UnblockingChainResult(
                target_unit_index=-1,
                target_selection_mode="none",
                segments=[],
                total_chain_duration=0.0,
                total_execution_duration=0.0,
                total_ready_to_start_delays=0.0,
                initial_delay=0.0,
                diagnostics=["No units recorded start events in this log session."],
                unreconstructed_notes=["Session contains no executed units."],
            )

        # 1. Target unit selection
        if target_unit_index is not None:
            if target_unit_index not in self.units:
                raise ValueError(f"Requested target unit {target_unit_index} does not exist in registry")
            target_unit = self.units[target_unit_index]
            target_mode = "explicit_flag"
            if target_unit.start_elapsed is None:
                diagnostics.append(f"Target unit {target_unit_index} was registered but never started.")
        else:
            # Check for explicitly requested units that finished
            finished_requested = [
                u for u in started_units if u.requested and u.finish_elapsed is not None
            ]
            if finished_requested:
                target_unit = max(finished_requested, key=lambda u: u.finish_elapsed or 0.0)
                target_mode = "requested_target"
            else:
                finished_units = [u for u in started_units if u.finish_elapsed is not None]
                if finished_units:
                    target_unit = max(finished_units, key=lambda u: u.finish_elapsed or 0.0)
                    target_mode = "latest_finish"
                else:
                    target_unit = max(started_units, key=lambda u: u.start_elapsed or 0.0)
                    target_mode = "latest_start"

        # 2. Backward walk along recorded unblock events
        segments_rev: List[UnblockingChainSegment] = []
        visited: Set[int] = set()

        curr_unit: Optional[UnitRecord] = target_unit
        phase_required = "finish"
        queue_start = self.session.unit_graph_finished or 0.0

        while curr_unit is not None:
            visited.add(curr_unit.index)

            start = curr_unit.start_elapsed
            if start is None:
                diagnostics.append(f"Unit {curr_unit.index} on unblocking chain has no start timestamp.")
                break

            full_finish = curr_unit.finish_elapsed

            if phase_required == "rmeta" and curr_unit.rmeta_elapsed is not None:
                effective_end = curr_unit.rmeta_elapsed
            else:
                effective_end = curr_unit.finish_elapsed if curr_unit.finish_elapsed is not None else start

            exec_duration = effective_end - start
            if exec_duration < 0.0:
                diagnostics.append(
                    f"Timestamp anomaly: Unit {curr_unit.index} finish/rmeta ({effective_end:.4f}s) "
                    f"precedes start ({start:.4f}s)."
                )

            unblock_ev = curr_unit.unblocked_by
            timestamp_inconsistent = False

            if unblock_ev is not None:
                unblock_idx = unblock_ev.unblocker_index
                unblock_kind = unblock_ev.kind
                unblock_time = unblock_ev.elapsed
                ready_delay = start - unblock_time
                is_root = False
                note = ""

                if ready_delay < -0.0001:
                    timestamp_inconsistent = True
                    diagnostics.append(
                        f"Timestamp anomaly: Unit {curr_unit.index} started at {start:.4f}s, "
                        f"before unblocking event at {unblock_time:.4f}s."
                    )
            else:
                is_root = True
                unblock_idx = None
                unblock_kind = None
                unblock_time = None
                ready_delay = start - queue_start

                if not curr_unit.dependencies:
                    note = "DAG root (no registered dependencies)"
                else:
                    fresh_deps = [
                        d for d in curr_unit.dependencies
                        if self.units.get(d) and self.units[d].status == "fresh"
                    ]
                    if len(fresh_deps) == len(curr_unit.dependencies):
                        note = "Unblocked by fresh dependencies (Cargo omits unblock events for fresh units)"
                        unreconstructed_notes.append(
                            f"Unit {curr_unit.display_name} (index {curr_unit.index}) was unblocked by "
                            f"fresh dependency units {[self.units[d].name for d in fresh_deps]}, but Cargo's "
                            f"event stream omits unblock events for fresh units."
                        )
                    else:
                        note = "Missing unblocking event in log (unrecorded predecessor)"
                        unreconstructed_notes.append(
                            f"Unit {curr_unit.display_name} (index {curr_unit.index}) had dependencies "
                            f"but no unblocking event was logged."
                        )

            # Check if this unit is a direct registered dependency of the unit that was unblocked
            is_registered = False
            if segments_rev:
                successor_idx = segments_rev[-1].unit_index
                successor = self.units.get(successor_idx)
                if successor and curr_unit.index in successor.dependencies:
                    is_registered = True

            seg = UnblockingChainSegment(
                step=0,
                unit_index=curr_unit.index,
                unit_name=curr_unit.display_name,
                target_kind=curr_unit.target_kind,
                phase_unblocked=phase_required,
                start_elapsed=start,
                effective_end_elapsed=effective_end,
                full_finish_elapsed=full_finish,
                execution_duration=exec_duration,
                ready_to_start_delay=ready_delay,
                unblocked_by_index=unblock_idx,
                unblocked_by_kind=unblock_kind,
                unblock_time=unblock_time,
                is_observed_root=is_root,
                is_registered_dep_of_successor=is_registered,
                timestamp_inconsistent=timestamp_inconsistent,
                note=note,
            )
            segments_rev.append(seg)

            if unblock_ev is None:
                break

            prev_idx = unblock_ev.unblocker_index
            if prev_idx in visited:
                diagnostics.append(f"Cycle in unblocking chain detected at unit index {prev_idx}")
                break

            prev_unit = self.units.get(prev_idx)
            if prev_unit is None:
                diagnostics.append(f"Unblock event referenced unregistered unit index {prev_idx}")
                break

            phase_required = unblock_ev.kind
            curr_unit = prev_unit

        segments = list(reversed(segments_rev))
        for i, s in enumerate(segments, start=1):
            s.step = i

        total_exec = sum(s.execution_duration for s in segments)
        total_delays = sum(s.ready_to_start_delay for s in segments)
        initial_delay = segments[0].ready_to_start_delay if segments else 0.0

        if segments:
            first_start = segments[0].start_elapsed
            last_end = segments[-1].effective_end_elapsed
            total_duration = (last_end - first_start) + initial_delay
        else:
            total_duration = 0.0

        return UnblockingChainResult(
            target_unit_index=target_unit.index,
            target_selection_mode=target_mode,
            segments=segments,
            total_chain_duration=total_duration,
            total_execution_duration=total_exec,
            total_ready_to_start_delays=total_delays,
            initial_delay=initial_delay,
            diagnostics=diagnostics,
            unreconstructed_notes=unreconstructed_notes,
        )


def format_text_report(
    parser: BuildAnalysisParser,
    analyzer: CriticalPathAnalyzer,
    rebuild_chains: Dict[int, RebuildChain],
    chain_result: UnblockingChainResult,
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

    # 3. Dependency Graph Disambiguation
    lines.append("## 3. DEPENDENCY GRAPH INTERPRETATION")
    lines.append("  Distinguishing Registered, Effective Scheduling, and Rebuild-Causality Edges:")
    for idx, u in sorted(units.items()):
        reg_deps = [f"unit {d} ({units[d].name})" for d in u.dependencies if d in units]
        eff_deps = [
            f"unit {ed.dep_index} ({units[ed.dep_index].name}, {ed.required_artifact}{', synthetic' if ed.is_synthetic else ''})"
            for ed in u.effective_dependencies if ed.dep_index in units
        ]
        unblock_str = (
            f"unit {u.unblocked_by.unblocker_index} ({units[u.unblocked_by.unblocker_index].name}, {u.unblocked_by.kind} @ {u.unblocked_by.elapsed:.3f}s)"
            if u.unblocked_by and u.unblocked_by.unblocker_index in units
            else ("none (root or fresh unblocker)" if u.dependencies else "none (DAG root)")
        )

        lines.append(f"  * Unit {idx} [{u.display_name}]:")
        lines.append(f"      Registered Direct Dependencies:   {', '.join(reg_deps) if reg_deps else 'none'}")
        lines.append(f"      Effective Scheduling Prerequisites: {', '.join(eff_deps) if eff_deps else 'none'}")
        lines.append(f"      Recorded Dynamic Unblocker:        {unblock_str}")
    lines.append("")

    # 4. Rebuild Reason Chains
    lines.append("## 4. REBUILD REASON ATTRIBUTION CHAINS")
    if not rebuild_chains:
        lines.append("  No rebuilt units (all units fresh / up-to-date).")
    else:
        for idx, chain in sorted(rebuild_chains.items()):
            unit = units[idx]
            root_desc = chain.root_cause_summary
            chain_str = " -> ".join(f"unit {i} ({units[i].name})" for i in chain.full_chain)

            flag = f" [{chain.chain_category.upper()}]"
            lines.append(f"  * Unit {idx} [{unit.display_name}] status={chain.status}{flag}:")
            lines.append(f"      Causal Chain: {chain_str}")
            lines.append(f"      Root Invalidation Cause: {root_desc}")
            if chain.stale_path:
                lines.append(f"      Invalidating File Path:  {chain.stale_path}")
            if chain.is_ambiguous and chain.ambiguity_reason:
                lines.append(f"      Ambiguity Diagnostic:    {chain.ambiguity_reason}")
    lines.append("")

    # 5. Observed Dependency-Unblocking Chain
    lines.append("## 5. OBSERVED DEPENDENCY-UNBLOCKING CHAIN")
    if not chain_result.segments:
        lines.append("  No unblocking chain reconstructed.")
    else:
        term = units.get(chain_result.target_unit_index)
        term_desc = term.display_name if term else f"unit {chain_result.target_unit_index}"
        lines.append(f"  Target Unit: {term_desc} (index {chain_result.target_unit_index}, selection: {chain_result.target_selection_mode})")
        lines.append("")
        lines.append(
            f"  {'Step':<5} {'Unit Name':<28} {'Phase':<8} {'Start (s)':<10} "
            f"{'End (s)':<10} {'Exec Time':<11} {'Ready Delay':<12} {'Unblocked By'}"
        )
        lines.append("  " + "-" * 110)

        for s in chain_result.segments:
            if s.unblocked_by_index is not None:
                dep_flag = " (direct)" if s.is_registered_dep_of_successor else " (synthetic/indirect)"
                unblock_str = f"unit {s.unblocked_by_index} ({s.unblocked_by_kind} @ {s.unblock_time:.3f}s){dep_flag}"
            else:
                unblock_str = "DAG root / initial"

            warn_flag = "!" if s.timestamp_inconsistent else " "
            lines.append(
                f" {warn_flag}{s.step:<4} {s.unit_name[:26]:<28} {s.phase_unblocked:<8} "
                f"{s.start_elapsed:<10.3f} {s.effective_end_elapsed:<10.3f} "
                f"{s.execution_duration:<10.3f}s {s.ready_to_start_delay:<11.3f}s {unblock_str}"
            )
            if s.note:
                lines.append(f"        Note: {s.note}")

    lines.append("")

    # 6. Timing Breakdown
    lines.append("## 6. TIMING AND CHAIN BUDGET")
    total_dur = max(0.0001, chain_result.total_chain_duration)
    lines.append(f"  Total Observed Chain Duration:       {chain_result.total_chain_duration:.4f}s")
    lines.append(f"  - Active Unit Execution on Chain:    {chain_result.total_execution_duration:.4f}s "
                 f"({chain_result.total_execution_duration / total_dur * 100:.1f}%)")
    lines.append(f"  - Cumulative Ready-to-Start Delays:  {chain_result.total_ready_to_start_delays:.4f}s "
                 f"({chain_result.total_ready_to_start_delays / total_dur * 100:.1f}%)")
    lines.append(f"    (Initial Queue Startup Delay:      {chain_result.initial_delay:.4f}s)")
    lines.append("")
    lines.append("  Note on Ready-to-Start Delays:")
    lines.append("    Delays between unblocking and execution start conflate jobserver token contention (-j),")
    lines.append("    job queue priority ordering, and OS process fork/exec overhead. Cargo does not log")
    lines.append("    token acquire/release events, so this interval cannot be definitively split into")
    lines.append("    dependency waiting vs worker saturation without additional instrumentation.")
    lines.append("")

    # 7. Unreconstructed / Missing Information
    lines.append("## 7. EVENTS & INFORMATION THAT COULD NOT BE RECONSTRUCTED")
    all_unreconstructed: List[str] = []

    if parser.malformed_lines:
        all_unreconstructed.append(
            f"Parser encountered {len(parser.malformed_lines)} malformed/unparseable log lines."
        )
    if parser.unrecognized_events:
        all_unreconstructed.append(
            f"Unrecognized event reasons ignored: {list(set(parser.unrecognized_events))}"
        )
    if chain_result.diagnostics:
        all_unreconstructed.extend(chain_result.diagnostics)
    if chain_result.unreconstructed_notes:
        all_unreconstructed.extend(chain_result.unreconstructed_notes)

    # Check for units with dependencies lacking an observed unblocker
    for u in units.values():
        if u.start_elapsed is not None and u.dependencies and u.unblocked_by is None:
            all_unreconstructed.append(
                f"Unit {u.display_name} (index {u.index}) started at {u.start_elapsed:.3f}s but has "
                f"no unblocking event in the log. Upstream dependencies were omitted from unblocking telemetry."
            )

    if fresh_count > 0:
        all_unreconstructed.append(
            f"{fresh_count} units were fresh (cached). Cargo emits no start, finish, or unblock events for fresh units, "
            f"making their fingerprint verification time unmeasurable from log events alone."
        )

    all_unreconstructed.append(
        "Scheduler queue state (jobserver tokens, running jobs count, OS process fork latency) "
        "is not logged by Cargo, meaning scheduler delays cannot be definitively assigned to job token starvation vs OS delay."
    )

    if not all_unreconstructed:
        lines.append("  None. All events and relationships were fully reconstructed.")
    else:
        for item in sorted(set(all_unreconstructed)):
            lines.append(f"  * {item}")

    lines.append("")
    lines.append("=" * 80)
    return "\n".join(lines)


def serialize_analysis(
    parser: BuildAnalysisParser,
    rebuild_chains: Dict[int, RebuildChain],
    chain_result: UnblockingChainResult,
) -> Dict[str, Any]:
    """Serializes analysis output into clean, structured, JSON-safe dictionaries."""
    return {
        "session": asdict(parser.session),
        "units": {str(k): asdict(v) for k, v in parser.units.items()},
        "rebuild_chains": {str(k): asdict(v) for k, v in rebuild_chains.items()},
        "unblocking_chain": asdict(chain_result),
        "diagnostics": {
            "malformed_lines": parser.malformed_lines,
            "parse_warnings": parser.parse_warnings,
            "unrecognized_events": parser.unrecognized_events,
            "chain_diagnostics": chain_result.diagnostics,
            "unreconstructed_notes": chain_result.unreconstructed_notes,
        },
    }


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Analyze Cargo -Zbuild-analysis logs for critical path and rebuild attribution."
    )
    arg_parser.add_argument("log_file", help="Path to Cargo JSONL build analysis log file (or '-' for stdin)")
    arg_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output report format (default: text)"
    )
    arg_parser.add_argument(
        "--target", type=int, default=None, help="Specific UnitIndex to trace critical path for (default: terminal target)"
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
        data = serialize_analysis(parser, rebuild_chains, crit_path)
        print(json.dumps(data, indent=2))
    else:
        report = format_text_report(parser, analyzer, rebuild_chains, crit_path)
        print(report)


if __name__ == "__main__":
    main()
