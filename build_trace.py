#!/usr/bin/env python3
"""Observed unblocking chains and rebuild attribution for Cargo -Zbuild-analysis logs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
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
    """A queue candidate; an artifact can be known conditionally on its presence."""

    dep_index: int
    required_artifact: str  # "metadata" | "all" | "unknown"
    is_synthetic: bool      # Transitive candidate absent from registered direct edges
    certainty: str = "known"  # "known" | "inferred" | "unknown"


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
    registered: bool = True
    features: List[str] = field(default_factory=list)

    # 1. Registered dependency graph (emitted in UnitRegistered)
    dependencies: List[int] = field(default_factory=list)

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
    def requires_upstream_objects(self) -> Optional[bool]:
        """Return only what UnitRegistered preserves of Cargo's object requirement."""
        if self.mode in ("test", "doctest"):
            return True
        if self.target_kind in ("bin", "test", "bench", "build-script"):
            return True
        # `lib` and `example` omit crate types; `check` also omits its test flag.
        return None

    @property
    def duration(self) -> Optional[float]:
        if self.start_elapsed is not None and self.finish_elapsed is not None:
            return self.finish_elapsed - self.start_elapsed
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
    chain_category: str
    root_cause_index: Optional[int]
    root_cause_summary: str
    full_chain: List[int]  # [unit_index, ..., last inspected dependency]
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
    effective_end_elapsed: Optional[float]
    full_finish_elapsed: Optional[float]
    execution_duration: Optional[float]
    ready_to_start_delay: Optional[float]
    unblocked_by_index: Optional[int]
    unblocked_by_kind: Optional[str]
    unblock_time: Optional[float]
    is_observed_root: bool
    unblocker_relationship: str  # "direct" | "inferred_synthetic" | "unknown" | "none"
    timestamp_inconsistent: bool = False
    note: str = ""


@dataclass
class UnblockingChainResult:
    target_unit_index: int
    target_selection_mode: str
    segments: List[UnblockingChainSegment]
    status: str
    total_chain_duration: Optional[float]
    total_execution_duration: Optional[float]
    total_ready_to_start_delays: Optional[float]
    initial_interval: Optional[float]
    diagnostics: List[str]
    unreconstructed_notes: List[str]


def unit_index(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError(f"Invalid unit index: {value!r}")
    return value


def timestamp(value: Any, *, allow_negative: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 and not allow_negative):
        raise ValueError(f"Invalid timestamp: {value!r}")
    return float(value)


def finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite JSON number: {value}")
    return number


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
            if "@" in right:
                name, version = right.rsplit("@", 1)
                return name, version
            crate_name = left.rstrip("/").split("/")[-1]
            return (crate_name, right)

    tokens = package_id_str.split()
    if len(tokens) >= 2:
        return (tokens[0], tokens[1])
    return (package_id_str, "unknown")


def parse_dirty_reason(cause: Optional[Dict[str, Any]], status: Optional[str]) -> ParsedDirtyReason:
    """Parse Cargo's flattened DirtyReason/FsStatus/StaleItem records."""
    if status == "new":
        return ParsedDirtyReason(kind="fresh-build", summary="fresh build (no previous artifact)")
    if status == "fresh":
        return ParsedDirtyReason(kind="up-to-date", summary="up-to-date (cached)")
    if not cause or not isinstance(cause, dict):
        return ParsedDirtyReason(kind="unspecified", summary="dirty (unspecified reason)")

    dirty_reason = cause.get("dirty_reason", "unknown")

    # 1. Explicit dependency pointers
    if dirty_reason == "unit-dependency-info-changed":
        dep_idx = unit_index(cause["unit"])
        return ParsedDirtyReason(
            kind=dirty_reason,
            summary=f"dependency info changed: unit {dep_idx}",
            explicit_dep_unit=dep_idx,
            details=cause,
        )

    if dirty_reason == "fs-status-outdated":
        fs_status = cause.get("fs_status")
        if fs_status in ("stale-dependency", "stale-dep-fingerprint"):
            dep_idx = unit_index(cause["unit"])
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
                summary = f"{summary_item}: {stale}"
            elif stale_item == "changed-env":
                summary = f"env var '{cause.get('var')}' changed"
            else:
                summary = summary_item

            return ParsedDirtyReason(
                kind=f"fs-status-outdated:stale-item:{stale_item or 'unknown'}",
                summary=summary,
                stale_path=str(stale) if stale else None,
                reference_path=str(reference) if reference else None,
                stale_item=str(stale_item) if stale_item else None,
                stale_mtime=timestamp(stale_mtime, allow_negative=True) if stale_mtime is not None else None,
                reference_mtime=timestamp(ref_mtime, allow_negative=True) if ref_mtime is not None else None,
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
        self.validation_errors: List[str] = []
        self.inference_diagnostics: List[str] = []
        self._session_events: Set[str] = set()
        self._unidentified_session = False
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
            raw_entry = json.loads(line, parse_float=finite_json_float, parse_constant=finite_json_float)
        except ValueError as e:
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

        try:
            if "run_id" in raw_entry:
                if not isinstance(run_id, str) or not run_id:
                    raise ValueError("Invalid run_id")
                if self._unidentified_session:
                    raise ValueError("Identified record after an unidentified prefix; event excluded")
                if self.session.run_id is not None and run_id != self.session.run_id:
                    raise ValueError(f"Mixed sessions: expected run_id {self.session.run_id!r}, got {run_id!r}; event excluded")
            elif self.session.run_id is not None:
                raise ValueError(f"Missing run_id for identified session {self.session.run_id!r}; event excluded")
            self._validate_event(reason, raw_entry)
            self._handle_event(reason, raw_entry)
            # Only accepted records establish the session's identity policy.
            if run_id is None:
                self._unidentified_session = True
            else:
                self.session.run_id = run_id
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            msg = f"Malformed payload for reason '{reason}': {e}"
            self.malformed_lines.append((line_no, raw_line, msg))
            if self.strict:
                raise LogParseError(msg, line_no, raw_line) from e

    def _validate_event(self, reason: str, data: Dict[str, Any]) -> None:
        if reason == "build-started":
            command = data.get("command", [])
            if not isinstance(command, list) or any(not isinstance(arg, str) for arg in command):
                raise ValueError("command must be an array of strings")
        if reason in ("resolution-started", "resolution-finished", "unit-graph-started", "unit-graph-finished",
                      "unit-started", "unit-rmeta-finished", "unit-finished",
                      "unit-section-started", "unit-section-finished"):
            data["elapsed"] = timestamp(data["elapsed"])
        if reason in ("unit-registered", "unit-fingerprint", "unit-started", "unit-rmeta-finished",
                      "unit-finished", "unit-section-started", "unit-section-finished"):
            unit_index(data["index"])
        if reason in ("unit-section-started", "unit-section-finished"):
            if not isinstance(data["section"], str):
                raise ValueError("section must be a string")
        for key in ("dependencies", "unblocked"):
            if reason not in ("unit-registered", "unit-rmeta-finished", "unit-finished") or key not in data:
                continue
            values = data[key]
            if not isinstance(values, list):
                raise ValueError(f"{key} must be an array")
            for value in values:
                unit_index(value)
            if key == "unblocked" and len(set(values)) != len(values):
                raise ValueError(f"Duplicate indices in {key}")
        if reason == "unit-registered":
            for key in ("package_id", "mode", "platform"):
                if not isinstance(data[key], str) or not data[key]:
                    raise ValueError(f"Invalid {key}")
            for key in ("name", "kind"):
                if not isinstance(data["target"][key], str) or not data["target"][key]:
                    raise ValueError(f"Invalid target {key}")
            if type(data.get("requested", False)) is not bool:
                raise ValueError("requested must be a boolean")
            features = data.get("features", [])
            if not isinstance(features, list) or any(not isinstance(f, str) for f in features):
                raise ValueError("features must be an array of strings")
        if reason == "unit-fingerprint":
            if data["status"] not in ("new", "fresh", "dirty"):
                raise ValueError("Invalid fingerprint status")
            if data.get("cause") is not None and not isinstance(data["cause"], dict):
                raise ValueError("cause must be an object")
            if data.get("cause") is not None and not isinstance(data["cause"].get("dirty_reason"), str):
                raise ValueError("Missing or invalid dirty_reason")
            if data["status"] != "dirty" and data.get("cause") is not None:
                raise ValueError("A new or fresh fingerprint cannot have a dirty cause")
        if reason in ("unit-started", "unit-rmeta-finished", "unit-finished"):
            unit = self.units.get(data["index"])
            attr = {"unit-started": "start_elapsed", "unit-rmeta-finished": "rmeta_elapsed",
                    "unit-finished": "finish_elapsed"}[reason]
            if unit and getattr(unit, attr) is not None:
                raise ValueError(f"Repeated {reason} for unit {unit.index}")
            for idx in data.get("unblocked", []):
                target = self.units.get(idx)
                if target and target.unblocked_by is not None:
                    raise ValueError(f"Repeated or contradictory unblocking event for unit {idx}")

    def _problem(self, message: str) -> None:
        self.validation_errors.append(message)
        if self.strict:
            raise LogParseError(message, 0, "")

    def _handle_event(self, reason: str, data: Dict[str, Any]) -> None:
        """Apply a validated record without coercing its indices or metadata."""
        if reason in ("build-started", "resolution-started", "resolution-finished",
                      "unit-graph-started", "unit-graph-finished"):
            if reason in self._session_events:
                raise ValueError(f"Repeated {reason}")
            self._session_events.add(reason)
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
            self.session.resolution_started = data["elapsed"]

        elif reason == "resolution-finished":
            self.session.resolution_finished = data["elapsed"]

        elif reason == "unit-graph-started":
            self.session.unit_graph_started = data["elapsed"]

        elif reason == "unit-graph-finished":
            self.session.unit_graph_finished = data["elapsed"]

        elif reason == "unit-registered":
            index = data["index"]
            pkg_id_str = data["package_id"]
            name, version = parse_package_id(pkg_id_str)
            target = data["target"]
            target_name = target["name"]
            target_kind = target["kind"]
            mode = data["mode"]
            platform = data["platform"]
            requested = data.get("requested", False)
            features = data.get("features", [])
            deps = data.get("dependencies", [])

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
            previous = self.units.get(index)
            if previous and previous.registered:
                fields = ("package_id", "target_name", "target_kind", "mode", "platform",
                          "requested", "features", "dependencies")
                conflict = any(getattr(previous, key) != getattr(unit, key) for key in fields)
                raise ValueError(f"{'Conflicting' if conflict else 'Duplicate'} registration for unit {index}")
            if previous:
                # Preserve execution events recovered before registration.
                for key in ("status", "cause_raw", "parsed_cause", "start_elapsed", "rmeta_elapsed",
                            "finish_elapsed", "unblocked_by", "unblocked_units", "unblocked_rmeta_units"):
                    setattr(unit, key, getattr(previous, key))
            self.units[index] = unit

        elif reason == "unit-fingerprint":
            index = data["index"]
            parsed_cause = parse_dirty_reason(data.get("cause"), data["status"])
            unit = self._ensure_unit(index)
            if unit.status is not None:
                raise ValueError(f"Repeated fingerprint for unit {index}")
            unit.status = data["status"]
            unit.cause_raw = data.get("cause")
            unit.parsed_cause = parsed_cause

        elif reason == "unit-started":
            index = data["index"]
            unit = self._ensure_unit(index)
            unit.start_elapsed = data["elapsed"]

        elif reason == "unit-rmeta-finished":
            index = data["index"]
            unit = self._ensure_unit(index)
            elapsed = data["elapsed"]
            unit.rmeta_elapsed = elapsed
            unblocked = data.get("unblocked", [])
            unit.unblocked_rmeta_units = unblocked

            for target_idx in unblocked:
                target_unit = self._ensure_unit(target_idx)
                target_unit.unblocked_by = UnblockEvent(
                    unblocker_index=index,
                    kind="rmeta",
                    elapsed=elapsed,
                )

        elif reason == "unit-finished":
            index = data["index"]
            unit = self._ensure_unit(index)
            elapsed = data["elapsed"]
            unit.finish_elapsed = elapsed
            unblocked = data.get("unblocked", [])
            unit.unblocked_units = unblocked

            for target_idx in unblocked:
                target_unit = self._ensure_unit(target_idx)
                target_unit.unblocked_by = UnblockEvent(
                    unblocker_index=index,
                    kind="finish",
                    elapsed=elapsed,
                )

        elif reason in ("unit-section-started", "unit-section-finished"):
            self._ensure_unit(data["index"])

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
                registered=False,
            )
            self.parse_warnings.append(f"Event referenced unit {index} before 'unit-registered'")
        return self.units[index]

    def _post_process(self) -> None:
        for start_key, end_key in (("resolution_started", "resolution_finished"),
                                   ("unit_graph_started", "unit_graph_finished"),
                                   ("resolution_finished", "unit_graph_started")):
            start, end = getattr(self.session, start_key), getattr(self.session, end_key)
            if start is not None and end is not None and end < start:
                self._problem(f"Timestamp anomaly: {end_key} precedes {start_key}")
        for unit in self.units.values():
            if not unit.registered:
                self._problem(f"Missing registration for unit {unit.index}")
            for dep_idx in unit.dependencies:
                dep_unit = self.units.get(dep_idx)
                if dep_unit is None or not dep_unit.registered:
                    self._problem(f"Unit {unit.index} depends on unregistered unit {dep_idx}")
            for begin, end, label in ((unit.start_elapsed, unit.finish_elapsed, "finish before start"),
                                      (unit.start_elapsed, unit.rmeta_elapsed, "metadata before start"),
                                      (unit.rmeta_elapsed, unit.finish_elapsed, "metadata after finish"),
                                      (self.session.unit_graph_finished, unit.start_elapsed, "start before unit graph finished")):
                if begin is not None and end is not None and end < begin:
                    self._problem(f"Timestamp anomaly: unit {unit.index}: {label}")
            if unit.start_elapsed is None and (unit.finish_elapsed is not None or unit.rmeta_elapsed is not None):
                self._problem(f"Unit {unit.index} has completion events but no start")
            if unit.status == "fresh" and unit.start_elapsed is not None:
                self._problem(f"Fresh unit {unit.index} has an execution start")
            pointer = unit.parsed_cause.explicit_dep_unit if unit.parsed_cause else None
            if pointer is not None and (pointer not in unit.dependencies or
                                       pointer not in self.units or not self.units[pointer].registered):
                self._problem(f"Unit {unit.index}: broken fingerprint reference to dependency {pointer}")
            if unit.unblocked_by and unit.start_elapsed is not None and unit.start_elapsed < unit.unblocked_by.elapsed:
                self._problem(f"Timestamp anomaly: unit {unit.index} started before its unblocking event")
            if unit.unblocked_by and unit.unblocked_by.kind == "rmeta":
                dep = self.units[unit.unblocked_by.unblocker_index]
                if (unit.mode != "build" or dep.mode != "build" or
                        unit.requires_upstream_objects is True or dep.requires_upstream_objects is True):
                    self._problem(f"Unit {unit.index}: metadata unblock contradicts known target/mode requirements")

        visited: Set[int] = set()
        active: Set[int] = set()

        def visit(idx: int) -> None:
            if idx in active:
                self._problem(f"Dependency graph cycle at unit {idx}")
                return
            if idx in visited or idx not in self.units:
                return
            active.add(idx)
            for dep in self.units[idx].dependencies:
                visit(dep)
            active.remove(idx)
            visited.add(idx)

        for idx in self.units:
            visit(idx)

        # JobQueue::enqueue seeds a map with filtered direct edges, then inserts
        # All edges recursively, stopping at an edge already present in that map.
        requires_objects = {u.index: u.requires_upstream_objects for u in self.units.values()}
        for unit in self.units.values():
            event = unit.unblocked_by
            if event and event.kind == "rmeta":
                dep = self.units[event.unblocker_index]
                if (unit.mode == dep.mode == "build" and
                        requires_objects[unit.index] is not True and requires_objects[dep.index] is not True):
                    # A Metadata edge can only exist when both object requirements
                    # are false. Merely producing rmeta would not prove this.
                    requires_objects[unit.index] = requires_objects[dep.index] = False
        for unit in self.units.values():
            effective: Dict[int, EffectiveSchedulingDep] = {}
            filter_unknown = any(self.units.get(d) is None or
                                 (self.units[d].target_kind not in ("lib", "example", "bench", "build-script")
                                  and self.units[d].mode != "docscrape")
                                 for d in unit.dependencies)
            for dep_idx in unit.dependencies:
                dep = self.units.get(dep_idx)
                artifact = "unknown"
                certainty = "unknown"
                if dep and dep.registered and (dep.target_kind in ("lib", "example", "bench", "build-script") or dep.mode == "docscrape"):
                    if (unit.mode in ("check", "test", "doc", "doctest", "docscrape", "run-custom-build")
                            or dep.mode in ("check", "test", "doc", "doctest", "docscrape", "run-custom-build")
                            or requires_objects[unit.index] is True or requires_objects[dep.index] is True):
                        artifact, certainty = "all", "known"
                    elif (unit.mode == dep.mode == "build" and
                          requires_objects[unit.index] is False and requires_objects[dep.index] is False):
                        artifact, certainty = "metadata", "inferred"
                effective[dep_idx] = EffectiveSchedulingDep(dep_idx, artifact, False, certainty)

            if requires_objects[unit.index] is not False:
                certainty = "inferred" if requires_objects[unit.index] is True and not filter_unknown else "unknown"

                def collect_transitive(curr_idx: int) -> None:
                    curr = self.units.get(curr_idx)
                    if curr is None:
                        return
                    for d in curr.dependencies:
                        if d in effective:
                            if certainty == "inferred":
                                effective[d].required_artifact = "all"
                            continue
                        effective[d] = EffectiveSchedulingDep(d, "all", d not in unit.dependencies, certainty)
                        collect_transitive(d)

                for dep_idx in unit.dependencies:
                    collect_transitive(dep_idx)

            unit.effective_dependencies = list(effective.values())
            if any(ed.certainty == "unknown" for ed in unit.effective_dependencies):
                self.inference_diagnostics.append(
                    f"Unit {unit.index}: scheduling inference uncertain; UnitRegistered omits crate types, "
                    "check's test flag, and artifact-dependency flags. Candidate edges are not an exact queue graph."
                )


def classify_unblocking_edge(unit: UnitRecord, unblocker_index: int) -> str:
    """Classify the predecessor of this unit, independently of chain position."""
    if unblocker_index in unit.dependencies:
        return "direct"
    if any(dep.dep_index == unblocker_index and dep.is_synthetic and dep.certainty == "inferred"
           for dep in unit.effective_dependencies):
        return "inferred_synthetic"
    return "unknown"


class CriticalPathAnalyzer:
    """Reconstructs observed unblocking chains and explicit rebuild causes."""

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
            if category == "unresolved_upstream":
                root_summary = "explicit dependency change; dependency is fresh here, earlier cause unavailable"

            results[index] = RebuildChain(
                unit_index=index,
                status="dirty",
                chain_category=category,
                root_cause_index=None if is_ambiguous else root_idx,
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
            if next_unit is None or not next_unit.registered:
                return (
                    chain,
                    True,
                    f"Explicit dirty-reason referenced unregistered unit index {dep_ptr}",
                    current.index,
                    "broken_reference",
                )
            if dep_ptr not in current.dependencies:
                return (chain, True,
                        f"Explicit cause from unit {current.index} points to unit {dep_ptr}, "
                        "which is not a registered dependency",
                        current.index, "broken_reference")
            if next_unit.status == "fresh":
                return (chain + [dep_ptr], True,
                        f"Dependency {dep_ptr} is fresh in this session; its earlier invalidation is not recorded here",
                        dep_ptr, "unresolved_upstream")

            current = next_unit

        terminal_root = current if current is not None else unit
        root_cause = terminal_root.parsed_cause
        cause_kind = root_cause.kind if root_cause else "unspecified"

        # Check for ambiguity at the chain termination point
        dirty_deps = [
            d for d in terminal_root.dependencies
            if self.units.get(d) and self.units[d].status in ("dirty", "new")
        ]

        if cause_kind == "forced":
            return (chain, True, "Forced rebuild; ordinary invalidation root is not established",
                    terminal_root.index, "forced_rebuild")
        if cause_kind == "fresh-build":
            return (chain, False, None, terminal_root.index, "fresh_build")

        local_kinds = (
            "rustc-changed", "features-changed", "declared-features-changed", "target-configuration-changed",
            "path-to-source-changed", "profile-configuration-changed", "rustflags-changed", "config-settings-changed",
            "compile-kind-changed", "local-lengths-changed", "precalculated-components-changed",
            "checksum-use-changed", "fingerprint-method-changed", "dep-info-output-changed",
            "rerun-if-changed-output-file-changed", "rerun-if-changed-output-paths-changed", "env-vars-changed",
            "env-var-changed", "local-fingerprint-type-changed", "number-of-dependencies-changed",
            "unit-dependency-name-changed",
        )
        stale_item = root_cause.stale_item if root_cause else None
        known_stale = stale_item in ("missing-file", "unable-to-read-file", "failed-to-read-metadata", "file-size-changed",
                                    "changed-file", "changed-checksum", "missing-checksum", "changed-env")
        if cause_kind not in local_kinds and not known_stale:
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
            return (chain, True, f"No specific invalidation root: {cause_kind}", terminal_root.index, "unresolved_local")

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
        started = [u for u in self.units.values() if u.start_elapsed is not None]
        requested = [u for u in self.units.values() if u.requested]
        diagnostics = list(self.parser.validation_errors)
        notes: List[str] = []
        if len(requested) > 1:
            notes.append("Automatic selection uses the latest completed requested target; other requested work is not represented.")
        unfinished = [u.index for u in started if u.finish_elapsed is None]
        if unfinished:
            notes.append(f"Units with no full finish event: {unfinished}. Session completion is not established.")
        incomplete_requested = [u.index for u in requested if u.status != "fresh" and u.finish_elapsed is None]
        if incomplete_requested:
            notes.append(f"Requested targets without completed execution: {incomplete_requested}.")

        if target_unit_index is not None:
            if target_unit_index not in self.units or not self.units[target_unit_index].registered:
                raise ValueError(f"Requested target unit {target_unit_index} does not exist in registry")
            target = self.units[target_unit_index]
            selection = "explicit_flag"
        else:
            completed_requested = [u for u in requested if u.start_elapsed is not None and u.finish_elapsed is not None]
            completed = [u for u in started if u.finish_elapsed is not None]
            pending_requested = [u for u in requested if u.status != "fresh"]
            if completed_requested:
                target = max(completed_requested, key=lambda u: (u.finish_elapsed, u.index))
                selection = "requested_target"
            elif pending_requested:
                target = max(pending_requested, key=lambda u: (u.start_elapsed if u.start_elapsed is not None else -1, u.index))
                selection = "requested_incomplete"
            elif completed:
                target = max(completed, key=lambda u: (u.finish_elapsed, u.index))
                selection = "latest_finish"
            elif started:
                target = max(started, key=lambda u: (u.start_elapsed, u.index))
                selection = "latest_start"
            else:
                target = None
                selection = "none"
        if target and target.finish_elapsed is not None:
            if any(u.finish_elapsed is not None and u.finish_elapsed > target.finish_elapsed for u in self.units.values()):
                notes.append("Other units finished after the selected target. This chain does not cover all Cargo work.")
        if target and target.start_elapsed is None:
            diagnostics.append(f"Target unit {target.index} was registered but never started (status: {target.status or 'unknown'}).")
        if target is None:
            diagnostics.append("No units recorded start events in this log session; Cargo wall-clock time is not zero.")

        segments_rev: List[UnblockingChainSegment] = []
        visited: Set[int] = set()
        current = target
        phase = "finish"
        incomplete = bool(self.parser.malformed_lines) or (not started and any(u.status != "fresh" for u in self.units.values()))
        while current is not None:
            if current.index in visited:
                diagnostics.append(f"Cycle in unblocking chain at unit {current.index}")
                incomplete = True
                break
            visited.add(current.index)
            start = current.start_elapsed
            end = current.rmeta_elapsed if phase == "rmeta" else current.finish_elapsed
            if start is None:
                if current is not target or current.status != "fresh":
                    incomplete = True
                diagnostics.append(f"Unit {current.index} on unblocking chain has no start timestamp.")
                break
            if end is None:
                incomplete = True
                diagnostics.append(f"Unit {current.index} on unblocking chain has no {phase} completion timestamp; duration unknown.")
            execution = end - start if end is not None else None
            event = current.unblocked_by
            delay = start - event.elapsed if event else None
            inconsistent = (execution is not None and execution < 0) or (delay is not None and delay < 0)
            note = ""
            if event is None:
                if not current.dependencies:
                    note = "No registered dependencies; readiness start is not logged"
                elif all(self.units.get(d) and self.units[d].status == "fresh" for d in current.dependencies):
                    note = "Only fresh dependencies; Cargo omits their unblock events, readiness start unknown"
                    notes.append(f"Unit {current.index}: fresh dependencies have no observed readiness transition.")
                else:
                    note = "Missing unblocking event in log (unrecorded predecessor)"
                    notes.append(f"Unit {current.index}: predecessor could not be reconstructed.")
                    incomplete = True
            segments_rev.append(UnblockingChainSegment(
                step=0, unit_index=current.index, unit_name=current.display_name,
                target_kind=current.target_kind, phase_unblocked=phase,
                start_elapsed=start, effective_end_elapsed=end, full_finish_elapsed=current.finish_elapsed,
                execution_duration=execution, ready_to_start_delay=delay,
                unblocked_by_index=event.unblocker_index if event else None,
                unblocked_by_kind=event.kind if event else None,
                unblock_time=event.elapsed if event else None, is_observed_root=event is None,
                unblocker_relationship=classify_unblocking_edge(current, event.unblocker_index) if event else "none",
                timestamp_inconsistent=inconsistent, note=note,
            ))
            if event is None:
                break
            current = self.units.get(event.unblocker_index)
            if current is None or not current.registered:
                diagnostics.append(f"Unblock event references unregistered unit {event.unblocker_index}")
                incomplete = True
                break
            phase = event.kind

        segments = list(reversed(segments_rev))
        for i, segment in enumerate(segments, start=1):
            segment.step = i
        if self.parser.validation_errors:
            status = "inconsistent"
        elif incomplete:
            status = "incomplete"
        elif not segments:
            status = "not_executed" if target else "no_execution"
        else:
            status = "complete"
        reliable = status == "complete"
        initial = None
        if segments and self.session.unit_graph_finished is not None:
            initial = segments[0].start_elapsed - self.session.unit_graph_finished
        return UnblockingChainResult(
            target_unit_index=target.index if target else -1, target_selection_mode=selection,
            status=status, segments=segments,
            total_chain_duration=segments[-1].effective_end_elapsed - segments[0].start_elapsed if reliable else None,
            total_execution_duration=sum(s.execution_duration for s in segments) if reliable else None,
            total_ready_to_start_delays=sum(s.ready_to_start_delay for s in segments if s.ready_to_start_delay is not None) if reliable else None,
            initial_interval=initial, diagnostics=diagnostics, unreconstructed_notes=notes,
        )


def format_seconds(value: Optional[float]) -> str:
    return f"{value:.4f}s" if value is not None else "unknown"


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
    lines.append("CARGO BUILD TRACE: OBSERVED UNBLOCKING CHAIN AND REBUILD CAUSES")
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
    lines.append(f"  Total Registered Units: {sum(u.registered for u in units.values())}")
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
            f"unit {ed.dep_index} ({units[ed.dep_index].name}, {ed.required_artifact}, {ed.certainty}{', synthetic' if ed.is_synthetic else ''})"
            for ed in u.effective_dependencies if ed.dep_index in units
        ]
        unblock_str = (
            f"unit {u.unblocked_by.unblocker_index} ({units[u.unblocked_by.unblocker_index].name}, {u.unblocked_by.kind} @ {u.unblocked_by.elapsed:.3f}s)"
            if u.unblocked_by and u.unblocked_by.unblocker_index in units
            else ("none (root or fresh unblocker)" if u.dependencies else "none (DAG root)")
        )

        lines.append(f"  * Unit {idx} [{u.display_name}]:")
        lines.append(f"      Registered Direct Dependencies:   {', '.join(reg_deps) if reg_deps else 'none'}")
        lines.append(f"      Inferred Scheduling Prerequisites: {', '.join(eff_deps) if eff_deps else 'none'}")
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
            reason_label = "Terminal Reason (root unknown)" if chain.is_ambiguous else "Root Invalidation Cause"
            lines.append(f"      {reason_label}: {root_desc}")
            if chain.is_ambiguous and chain.ambiguity_reason:
                lines.append(f"      Ambiguity Diagnostic:    {chain.ambiguity_reason}")
    lines.append("")

    # 5. Observed Dependency-Unblocking Chain
    lines.append("## 5. OBSERVED DEPENDENCY-UNBLOCKING CHAIN")
    lines.append(f"  Chain status: {chain_result.status}")
    lines.append(f"  Target index: {chain_result.target_unit_index}, selection: {chain_result.target_selection_mode}")
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
                dep_flag = f" ({s.unblocker_relationship.replace('_', ' ')})"
                unblock_str = f"unit {s.unblocked_by_index} ({s.unblocked_by_kind} @ {s.unblock_time:.3f}s){dep_flag}"
            else:
                unblock_str = "none observed; readiness unknown"

            warn_flag = "!" if s.timestamp_inconsistent else " "
            lines.append(
                f" {warn_flag}{s.step:<4} {s.unit_name[:26]:<28} {s.phase_unblocked:<8} "
                f"{s.start_elapsed:<10.3f} {format_seconds(s.effective_end_elapsed):<10} "
                f"{format_seconds(s.execution_duration):<11} {format_seconds(s.ready_to_start_delay):<12} {unblock_str}"
            )
            if s.note:
                lines.append(f"        Note: {s.note}")

    lines.append("")

    # 6. Timing Breakdown
    lines.append("## 6. TIMING AND CHAIN BUDGET")
    lines.append(f"  Observed Chain Span:                {format_seconds(chain_result.total_chain_duration)}")
    lines.append(f"  - Active Unit Execution on Chain:   {format_seconds(chain_result.total_execution_duration)}")
    measured_gaps = sum(s.ready_to_start_delay is not None for s in chain_result.segments)
    lines.append(f"  - Measured Ready-to-Start Gaps:      {format_seconds(chain_result.total_ready_to_start_delays)} ({measured_gaps} observed)")
    lines.append(f"  Initial Graph-to-Start Interval:    {format_seconds(chain_result.initial_interval)} (readiness unknown)")
    lines.append("  Chain span begins at its first observed start, excluding the initial interval.")
    lines.append("  Unavailable totals are unknown; known segment timestamps remain above.")
    lines.append("")
    lines.append("  Note on Ready-to-Start Delays:")
    lines.append("    Delays between unblocking and execution start conflate jobserver token contention (-j),")
    lines.append("    job queue priority ordering, and OS process fork/exec overhead. Cargo does not log")
    lines.append("    token acquire/release events, so this interval cannot be definitively split into")
    lines.append("    token waiting, dispatch, and event-loop overhead without additional instrumentation.")
    lines.append("")

    # 7. Unreconstructed / Missing Information
    lines.append("## 7. EVENTS & INFORMATION THAT COULD NOT BE RECONSTRUCTED")
    all_unreconstructed: List[str] = []

    if parser.malformed_lines:
        all_unreconstructed.append(
            f"Parser encountered {len(parser.malformed_lines)} malformed/unparseable log lines."
        )
        all_unreconstructed.extend(f"Line {n}: {message}" for n, _, message in parser.malformed_lines)
    all_unreconstructed.extend(parser.parse_warnings)
    all_unreconstructed.extend(parser.inference_diagnostics)
    if parser.unrecognized_events:
        all_unreconstructed.append(
            f"Unrecognized event reasons ignored: {sorted(set(parser.unrecognized_events))}"
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
                "no observed dependency-ready transition in the log."
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
        "input_valid": not (parser.malformed_lines or parser.validation_errors),
        "session": asdict(parser.session),
        "units": {str(k): asdict(v) for k, v in parser.units.items()},
        "rebuild_chains": {str(k): asdict(v) for k, v in rebuild_chains.items()},
        "unblocking_chain": asdict(chain_result),
        "diagnostics": {
            "malformed_lines": [list(record) for record in parser.malformed_lines],
            "parse_warnings": parser.parse_warnings,
            "validation_errors": parser.validation_errors,
            "inference_diagnostics": parser.inference_diagnostics,
            "unrecognized_events": parser.unrecognized_events,
            "chain_diagnostics": chain_result.diagnostics,
            "unreconstructed_notes": chain_result.unreconstructed_notes,
        },
    }


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Analyze Cargo -Zbuild-analysis logs for observed unblocking chains and rebuild attribution."
    )
    arg_parser.add_argument("log_file", help="Path to Cargo JSONL build analysis log file (or '-' for stdin)")
    arg_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output report format (default: text)"
    )
    arg_parser.add_argument(
        "--target", type=int, default=None, help="Specific UnitIndex to trace (default: latest completed requested target)"
    )
    arg_parser.add_argument(
        "--strict", action="store_true", help="Reject malformed records, mixed sessions, and inconsistent graphs or timestamps"
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
        print(json.dumps(data, indent=2, allow_nan=False))
    else:
        report = format_text_report(parser, analyzer, rebuild_chains, crit_path)
        print(report)


if __name__ == "__main__":
    main()
