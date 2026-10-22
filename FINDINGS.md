# Findings: Incremental Build Critical-Path Analysis via Cargo `-Zbuild-analysis`

## Executive Summary

This prototype evaluated whether Cargo's existing `-Zbuild-analysis` structured logs (introduced under RFC 3493 / tracking issue [#15844](https://github.com/rust-lang/cargo/issues/15844)) provide sufficient telemetry to:
1. Reconstruct incremental build critical paths with metadata pipelining.
2. Attribute rebuild costs to root invalidation causes.
3. Quantify scheduling and concurrency bottlenecks.

The prototype implementation (`build_trace.py`) was validated against a multi-branch Cargo workspace across clean builds, no-change rebuilds, leaf modifications, shared dependency modifications, and variable concurrency limits (`-j 1` vs `-j 8`). Reconstructed execution timings demonstrate **100% agreement** with Cargo's internal timing engine (`cargo report timings`).

---

## 1. What Can Be Reconstructed Reliably

- **Exact Execution Windows & Metadata Pipelining**:
  - Cargo records `elapsed` timestamps (seconds from build start) for `UnitStarted`, `UnitRmetaFinished`, and `UnitFinished`.
  - Pipelined compilation is tracked: when a library emits `UnitRmetaFinished` with an `unblocked` list, downstream units depend only on the `.rmeta` duration rather than the full crate duration.
  - Unit durations calculated by `finish_elapsed - start_elapsed` match Cargo's internal centisecond-rounded report without divergence.

- **Explicit Rebuild Causal Chains**:
  - When a rebuild is triggered by an upstream dependency, Cargo's `DirtyReason` enum emits structured references containing the exact session-local `UnitIndex`:
    - `DirtyReason::UnitDependencyInfoChanged { unit }`
    - `FsStatus::StaleDependency { unit, .. }`
    - `FsStatus::StaleDepFingerprint { unit }`
  - These pointers allow recursive tracing from a target binary (e.g. `cli_app`) through intermediate libraries (`orchestrator`) back to the originating dirty crate (`string_parser`) and its filesystem invalidation event (`file modified: src/lib.rs`).

- **Scheduler Gaps on the Critical Path**:
  - The interval between an unblocking event (`unblock_time`) and downstream unit start (`start_elapsed`) is directly measurable (`gap = start - unblock_time`).
  - Serialized builds (`-j 1`) show measurable scheduler wait gaps (e.g. 54.8% of critical path duration) where ready units wait in the queue, whereas parallel builds (`-j 8`) reduce these gaps to < 1.5%.

---

## 2. What Can Only Be Estimated / Inferred

- **Scheduler Gap Root Causes**:
  - While the scheduler gap duration can be calculated precisely, the *reason* for the delay is not recorded. A gap could stem from:
    1. Job token exhaustion (concurrency ceiling reached).
    2. Queue priority ordering (other ready units picked first by the priority queue).
    3. OS process fork/exec latency when launching `rustc`.
    4. Main-thread Cargo event-loop latency.
  - Distinguishing between thread saturation and process spawning overhead is an estimate based on concurrency flags (`jobs`), not observed telemetry.

- **Non-Specific Dirty Causes**:
  - If a unit reports `NothingObvious`, `Stale`, or `Forced`, but has multiple upstream dependencies that rebuilt, any attribution of root cause to a particular upstream branch is a heuristic estimate. Cargo's log does not provide a definitive causal link for these variants.

- **Initial Clean Builds**:
  - Units built for the first time report `UnitFingerprint { status: "new", cause: None }`. The fact that it is a new compilation is certain, but whether it was triggered by a missing directory, a cleared target folder, or an added workspace member is not preserved.

---

## 3. What Information Is Missing

- **Unblocking Events from Fresh (Cached) Units (Critical Gap)**:
  - In `src/compiler/job_queue/mod.rs` and `src/compiler/timings/mod.rs`, fresh units run synchronously and bypass `self.active`. As a result, Cargo omits `UnitStarted`, `UnitRmetaFinished`, and `UnitFinished` for fresh units.
  - Crucially, when a fresh unit finishes in `JobQueue::finish`, it unblocks downstream dirty units in the dependency queue, but `timings.unit_finished` returns early without logging an unblocking event.
  - **Impact**: In typical incremental builds (where dependencies are fresh and only a leaf or intermediate crate rebuilds), the first rebuilding unit has **no logged unblocker**. An analyzer cannot distinguish whether this unit was ready from time zero or unblocked by fresh dependencies without inspecting the static dependency DAG.

- **Fresh Unit Fingerprint Check Latency**:
  - `UnitFingerprint` events do not record check duration or start/finish timestamps. In workspaces with hundreds of crates, the cumulative time Cargo spends checking mtimes, hashes, and dep-info for cached units cannot be determined from logs alone.

- **Dynamic Jobserver / Resource Telemetry**:
  - Logs report static `jobs` and `num_cpus` at build start, but omit dynamic jobserver token acquisition/release timestamps and running job counts over time.

---

## 4. Whether Additional Cargo Logging Events Are Justified

Based on this prototype, three targeted upstream logging improvements would enable complete, robust critical-path analysis:

1. **Log Unblocking Events for Fresh Units** (High Value, Negligible Overhead):
   - In `JobQueue::finish`, when a fresh unit unblocks downstream units, emit either:
     - `UnitFinished { index, elapsed, unblocked }` for the fresh unit, or
     - A dedicated `UnitUnblocked { index, unblocked_by, phase }` event.
   - This eliminates the blind spot at the beginning of incremental build critical paths.

2. **Add Token Wait Telemetry to `UnitStarted`** (High Value):
   - Include `wait_token_elapsed: f64` or `queue_delay: f64` in `UnitStarted`. This would measure how long a unit waited for a jobserver token after becoming ready, definitively separating job slot starvation from rustc process launch overhead.

3. **Record Duration in `UnitFingerprint`** (Medium Value):
   - Adding `elapsed: f64` to `UnitFingerprint` would measure the wall-clock progression of freshness evaluation across large dependency trees.
