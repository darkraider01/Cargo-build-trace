# Technical Findings: Incremental Build Critical-Path Analysis via Cargo `-Zbuild-analysis`

## Context & Objectives

Cargo's unstable `-Zbuild-analysis` flag (tracking issue [rust-lang/cargo#15844](https://github.com/rust-lang/cargo/issues/15844), based on RFC 3493) persists build metrics as session-keyed JSONL files in `~/.cargo/log/`.

This investigation evaluated whether these logs contain sufficient telemetry to:
1. Reconstruct incremental build critical paths under metadata pipelining.
2. Attribute incremental rebuild costs to root invalidation causes.
3. Quantify scheduling and concurrency bottlenecks.

The findings below are divided into:
- **Directly Observed Facts** (verified from real JSONL logs).
- **Behavior Verified Against Cargo Source** (confirmed by reading `rust-lang/cargo` compiler code).
- **Inferences Supported by Experiments** (demonstrated across our 6 validation scenarios).
- **Unresolved Questions** (where current log telemetry runs out).
- **Possible Upstream Improvements** (targeted, minimal changes with compatibility and overhead assessments).

---

## 1. Directly Observed Facts

From inspecting generated `-Zbuild-analysis` logs across clean builds, no-change rebuilds, and incremental changes:

1. **Exact Start, Metadata, and Completion Timestamps**:
   - `unit-started`, `unit-rmeta-finished`, and `unit-finished` log an `elapsed: f64` timestamp (seconds since build invocation).
   - For executed units, unit compilation duration (`finish_elapsed - start_elapsed`) and rmeta duration (`rmeta_elapsed - start_elapsed`) are directly observable and match the centisecond values rendered in Cargo's `--timings` HTML reports within declared rounding tolerances.

2. **Explicit Dependency Invalidation Pointers**:
   - When a crate is rebuilt because a dependency changed, `unit-fingerprint`'s `cause` contains structured session-local `UnitIndex` references:
     - `{"dirty_reason": "unit-dependency-info-changed", "unit": <idx>}`
     - `{"dirty_reason": "fs-status-outdated", "fs_status": "stale-dep-fingerprint", "unit": <idx>}`
     - `{"dirty_reason": "fs-status-outdated", "fs_status": "stale-dependency", "unit": <idx>}`
   - When a crate is rebuilt due to a modified source file, the file path, reference artifact path, and file modification timestamps are recorded directly in `cause`:
     ```json
     {
       "dirty_reason": "fs-status-outdated",
       "fs_status": "stale-item",
       "stale_item": "changed-file",
       "stale": "/path/to/src/lib.rs",
       "stale_mtime": 1792703020682.713,
       "reference": "/path/to/.fingerprint/.../dep-lib-...",
       "reference_mtime": 1792703020276.0693
     }
     ```

3. **Dynamic Unblocking Lists**:
   - `unit-rmeta-finished` and `unit-finished` carry an `unblocked: Vec<UnitIndex>` field listing units whose remaining dependency count dropped to zero upon that completion.

4. **Fresh Units Emit No Timing Events**:
   - In a no-change rebuild, `unit-registered` and `unit-fingerprint { status: "fresh" }` are emitted, but no `unit-started`, `unit-rmeta-finished`, or `unit-finished` events appear.

---

## 2. Behavior Verified Against Cargo Source

Inspection of the Cargo codebase (`src/compiler/job_queue/mod.rs`, `src/compiler/timings/mod.rs`, `src/compiler/build_runner/mod.rs`, `src/compiler/fingerprint/mod.rs`) established the concrete mechanics behind these observations:

### A. The Synthetic Scheduling Dependency Rule
- **Source**: `src/compiler/job_queue/mod.rs:430-468` and `src/compiler/crate_type.rs:70`.
- In `UnitRegistered`, Cargo logs only direct declared dependencies (`dependencies: Vec<UnitIndex>`).
- However, when enqueuing work into the internal `DependencyQueue`, Cargo checks `unit.requires_upstream_objects()`. For non-rlib targets (binaries, integration tests, cdylibs), linking requires full upstream object files (`.rlib`), not just `.rmeta`.
- Because pipelining allowed intermediate libraries to depend only on `.rmeta`, Cargo runs `depend_on_deps_of_deps`: it walks all transitive dependencies of the linkable target and synthesizes direct `(dep, Artifact::All)` edges.
- **Why this matters**: In our test workspace, `cli_app` depends directly on `orchestrator`, which depends on `string_parser`. In the log, `string_parser` emits `UnitFinished` with `unblocked: [cli_app, leaf_tool]`. `cli_app` was unblocked by a transitive crate that was never listed in `cli_app`'s `unit-registered.dependencies`. Downstream analyzers that assume `unblocked` must be a direct registered dependency will find the data contradictory unless they account for Cargo's linkable synthetic dependencies.

### B. Why Fresh Units Omit Unblock Telemetry
- **Source**: `src/compiler/job_queue/mod.rs:1008-1035`, `src/compiler/job_queue/mod.rs:1190-1215`, and `src/compiler/timings/mod.rs:155-195`.
- Fresh units run synchronously on the main thread via `state.run_to_finish(job)` without spawning a worker thread or requesting a job token.
- Fresh jobs do not emit `Message::Run`, so `Timings::unit_start` is never called, and the unit is not added to `Timings.active`.
- When the fresh job completes, `JobQueue::finish()` calls `self.queue.finish(unit, &artifact)`. The internal dependency queue removes the edge and correctly computes `unblocked: Vec<&Unit>`.
- However, `JobQueue::finish()` then passes `unblocked` to `timings.unit_finished()` or `timings.unit_rmeta_finished()`. In both methods, the very first line is:
  ```rust
  let Some(unit) = self.active.remove(&id) else {
      return;
  };
  ```
  Because `id` is not in `self.active`, the method returns immediately. The `unblocked` slice is discarded, and no `UnitFinished` log message is written.
- **Consequence**: When an incremental build rebuilds a leaf or middle crate whose dependencies were cached/fresh, the rebuilt unit begins compiling without any recorded unblock event in the log.

---

## 3. Inferences Supported by Experiments

Across our 6 validation scenarios in `test_workspace`:

1. **Incremental Rebuild Causal Chains Can Be Traced Deterministically**:
   - In Scenario 4 (`string_parser/src/lib.rs` modified), `cli_app` was dirty because `orchestrator` was dirty (`stale-dep-fingerprint: unit 4`), which in turn was dirty because `string_parser` was dirty (`stale-dep-fingerprint: unit 5`), which in turn was dirty due to `changed-file: /path/to/string_parser/src/lib.rs`.
   - By following explicit pointers, the root cause was identified without consulting the static DAG or guessing which branch caused the rebuild.

2. **Metadata Pipelining Significantly Shortens the Effective Critical Path**:
   - In Scenario 1 and 4, downstream libraries started compiling as soon as upstream emitted `.rmeta` (`UnitRmetaFinished`).
   - Using full crate duration instead of rmeta duration overstates the critical path by the codegen/link time of upstream libraries (in our clean build, `string_parser` unblocked `orchestrator` at 0.038s, whereas full codegen took until 0.051s).

3. **Concurrency Bottlenecks Appear as Ready-to-Start Delays**:
   - In Scenario 5a (`-j 1`), cumulative ready-to-start delays accounted for **49.3%** of total build duration (0.341s delay vs 0.350s active execution).
   - In Scenario 5b (`-j 8`), ready-to-start delays dropped to **1.0%** (0.003s).
   - While the presence of delay is demonstrable, the exact cause (jobserver token starvation vs process spawning latency) cannot be proven without additional telemetry.

---

## 4. Questions That Are Still Unresolved

1. **Job Token Starvation vs. Process Spawn Overhead**:
   - When a unit has a 0.05s ready-to-start delay under high core counts, is it waiting for a jobserver token, or is the delay caused by `rustc` process creation and dynamic linking? The log records static `jobs: u32` at startup, but no dynamic queue or token states.
2. **Fresh Unit Verification Cost in Large Workspaces**:
   - `UnitFingerprint` logs status (`fresh`, `dirty`, `new`), but no timestamps or durations. In a workspace with 500 crates, the wall-clock time spent checking filesystem mtimes and hashes for cached crates cannot be measured from existing events.
3. **Non-Specific Dirty Invalidation with Multiple Rebuilt Dependencies**:
   - If a unit reports `nothing-obvious`, `stale`, or `forced`, and multiple upstream dependencies rebuilt, the log provides no mechanism to attribute which dependency caused the invalidation.

---

## 5. Possible Upstream Improvements

The following improvements would resolve these specific blind spots.

### Proposal 1: Log Unblocking Events for Fresh Units (High Priority)

- **The Missing Information**: When a fresh (cached) unit satisfies a dependency for a downstream crate, the unblocking transition and timestamp are lost.
- **Existing Event That Almost Provides It**: `UnitFinished { index, elapsed, unblocked }` and `UnitRmetaFinished { index, elapsed, unblocked }`.
- **Why Existing Telemetry Is Insufficient**: In `src/compiler/timings/mod.rs:159`, `timings.unit_finished` checks `self.active.get(&id)`. Because fresh units do not run as external commands, they are omitted from `self.active`, so `unblocked` is discarded silently.
- **Concrete Example**: In Scenario 3 (modifying only `leaf_tool`), `leaf_tool` depends on `string_parser` (which is fresh). `leaf_tool` starts at `0.051s`, but no log event explains why it started at `0.051s` rather than `0.000s`.
- **Smallest Useful Addition**:
  In `src/compiler/job_queue/mod.rs:1200` (`JobQueue::finish`), when `unblocked` is non-empty for a fresh unit, emit a lightweight event:
  ```rust
  LogMessage::UnitUnblocked {
      index: UnitIndex,
      unblocked_by: UnitIndex,
      phase: Artifact,
      elapsed: f64,
  }
  ```
  Alternatively, allow `timings.unit_finished` to emit `UnitFinished { index, elapsed, unblocked }` with `elapsed` even when `self.active` does not contain the unit.
- **Compatibility & Overhead**: Existing consumers (such as `cargo report timings`) already filter on `started: true` before rendering Gantt charts, so emitting unblocking events for fresh units will not create phantom zero-duration rows in HTML reports. The runtime cost is negligible (an existing in-memory `unblocked` vector is written to the log thread channel).

---

### Proposal 2: Record Queue/Token Wait Time in `UnitStarted` (Medium Priority)

- **The Missing Information**: The duration a ready unit spent waiting in `pending_queue` for an available jobserver token.
- **Existing Event That Almost Provides It**: `UnitStarted { index, elapsed }`.
- **Why Existing Telemetry Is Insufficient**: `elapsed` records only when the worker process was spawned. It does not record when the unit became ready to run.
- **Concrete Example**: In Scenario 5a (`-j 1`), units spend hundreds of milliseconds ready in queue before starting. Without token telemetry, an analyzer cannot prove whether this was caused by job starvation or process execution latency.
- **Smallest Useful Addition**:
  When a unit is dequeued into `pending_queue` in `JobQueue::spawn_work_if_possible`, record `ready_instant = Instant::now()`. When `run()` is called, compute `queue_wait = ready_instant.elapsed().as_secs_f64()` and include it in `UnitStarted`:
  ```rust
  UnitStarted {
      index: UnitIndex,
      elapsed: f64,
      queue_wait: Option<f64>,
  }
  ```
- **Compatibility & Overhead**: Adding an optional field is completely backward-compatible with serde-based consumers. Overhead is one `Instant::now()` call per compilation unit.

---

### Proposal 3: Expose Effective Scheduling Dependencies in `UnitRegistered` (Low Priority)

- **The Missing Information**: The distinction between registered direct dependencies and synthesized linking dependencies for non-rlib targets.
- **Existing Event That Almost Provides It**: `UnitRegistered { dependencies: Vec<UnitIndex> }`.
- **Why Existing Telemetry Is Insufficient**: `dependencies` only reflects the package manifest graph. Tools analyzing scheduling order must reimplement Cargo's private `depend_on_deps_of_deps` logic to understand why an unexpected unit appeared in `unblocked`.
- **Smallest Useful Addition**:
  Document the synthetic dependency rule in the `-Zbuild-analysis` RFC/documentation, or optionally include `synthetic_dependencies: Vec<UnitIndex>` in `UnitRegistered`.
- **Compatibility & Overhead**: Negligible (computed once during unit graph construction).
