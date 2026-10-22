# Cargo Build Trace

Cargo already exposes quite a bit of build information through `-Zbuild-analysis`. I wanted to see how far that data could go in explaining incremental rebuilds, particularly which dependency chains end up delaying a build.

This started as a small experiment to reconstruct those chains from the existing logs before asking Cargo to record anything new.

## What this is

This tool is a standalone Python script that consumes a Cargo `-Zbuild-analysis` JSONL session log and attempts to answer two practical questions:

1. **Why did a unit rebuild?** (Tracing the chain of invalidation from a target binary back to the root source change or configuration delta).
2. **Which work contributed to the observed build completion time?** (Reconstructing the observed sequence of task unblocking events that led to the final target completing).

These two questions are related, but they are not the same thing. A crate can trigger a rebuild without being on the longest execution path, and a crate on the critical path might simply be compiling slowly rather than causing downstream invalidations.

## Why I started looking into this

When a build feels sluggish after editing a file, `cargo build` doesn't tell you much beyond a list of compiling crates. Cargo's existing `-Zbuild-analysis` logs (tracked under [rust-lang/cargo#15844](https://github.com/rust-lang/cargo/issues/15844)) provide structured telemetry about rebuild reasons and compilation timings, but interpreting them during parallel builds is tricky:

- Summing compilation times does not equal wall-clock time because crates compile in parallel.
- Metadata pipelining means downstream crates start compiling once upstream emits `.rmeta`, before codegen or linking finishes.
- Linkable targets (like binaries) synthesize extra scheduling dependencies on transitive crates that do not appear as direct dependencies in the manifest.
- When multiple dependency branches rebuild, identifying which branch actually dictated completion time requires following the unblocking sequence.

Before proposing any new Cargo logging events, I wanted to see how much of this could already be reconstructed reliably from existing logs, and where the data genuinely runs out.

## What the prototype currently does

The prototype (`build_trace.py`) parses a `-Zbuild-analysis` log and performs:

- **Graph disambiguation**: Separates three distinct dependency graphs:
  - *Registered direct dependencies*: the crate edges declared in `unit-registered`.
  - *Effective scheduling prerequisites*: the full set of requirements enforced by Cargo's queue (including pipelined metadata vs. full artifact needs, and synthetic link dependencies for binaries).
  - *Rebuild causality*: explicit dependency pointers recorded in fingerprint causes (`UnitDependencyInfoChanged`, `StaleDependency`, `StaleDepFingerprint`).
- **Rebuild reason tracing**: Follows explicit dependency invalidations recursively to identify the root file modification or configuration change. If a chain terminates at a unit with a generic reason (e.g., `nothing-obvious`) while dependencies rebuilt, it flags the cause as ambiguous rather than guessing.
- **Observed unblocking chain reconstruction**: Walks backward from the terminal target along recorded `unblocked` events, accounting for metadata pipelining (rmeta duration vs. full crate duration).
- **Delay attribution**: Measures the ready-to-start delay between when a unit is unblocked and when it begins executing.
- **Missing telemetry diagnostics**: Flags when unblocking events are omitted (such as when all dependencies are fresh/cached).
- **Dual output**: Generates readable plain text reports or structured JSON (`--format json`).

## Getting started

### Requirements

- Python 3.9+ (standard library only, no external packages required).
- Rust nightly toolchain (e.g. `cargo +nightly` or setting `RUSTC_BOOTSTRAP=1` on dev builds) to enable `-Zbuild-analysis`.

### Generating a log

Configure Cargo to enable build analysis:

```bash
# In your project's .cargo/config.toml
[build.analysis]
enabled = true
```

Or set the environment variable:

```bash
export CARGO_BUILD_ANALYSIS_ENABLED=true
```

Run a build with `-Zbuild-analysis`:

```bash
cargo +nightly build -Zbuild-analysis
```

Cargo writes session logs to `~/.cargo/log/<run_id>.jsonl`.

### Running the analyzer

Analyze the latest log:

```bash
# Run the analyzer on a specific log file
python3 build_trace.py ~/.cargo/log/<run_id>.jsonl

# Or pipe a log directly via stdin
cat ~/.cargo/log/<run_id>.jsonl | python3 build_trace.py -

# Specify a target unit index if multiple targets built
python3 build_trace.py ~/.cargo/log/<run_id>.jsonl --target 0

# Emit structured JSON
python3 build_trace.py ~/.cargo/log/<run_id>.jsonl --format json
```

### Running tests and validation

```bash
# Run independent unit and synthetic graph tests
python3 -m unittest discover -s tests

# Run reproducible validation scenarios in an isolated workspace
python3 scripts/run_validation_scenarios.py
```

## Example output

### Rebuild reason attribution

From a scenario where `string_parser/src/lib.rs` was edited:

```text
## 4. REBUILD REASON ATTRIBUTION CHAINS
  * Unit 0 [cli_app v0.1.0 (cli_app:bin)] status=dirty [CONFIRMED_EXPLICIT]:
      Causal Chain: unit 0 (cli_app) -> unit 4 (orchestrator) -> unit 5 (string_parser)
      Root Invalidation Cause: changed-file modified: /path/to/string_parser/src/lib.rs
      Invalidating File Path:  /path/to/string_parser/src/lib.rs

  * Unit 5 [string_parser v0.1.0] status=dirty [LOCAL_INVALIDATION]:
      Causal Chain: unit 5 (string_parser)
      Root Invalidation Cause: changed-file modified: /path/to/string_parser/src/lib.rs
      Invalidating File Path:  /path/to/string_parser/src/lib.rs
```

Every step in this chain was backed by an explicit pointer in Cargo's `DirtyReason` telemetry.

### Observed unblocking chain

For the same build:

```text
## 5. OBSERVED DEPENDENCY-UNBLOCKING CHAIN
  Target Unit: cli_app v0.1.0 (cli_app:bin) (index 0, selection: requested_target)

  Step  Unit Name                    Phase    Start (s)  End (s)    Exec Time   Ready Delay  Unblocked By
  --------------------------------------------------------------------------------------------------------------
   1    string_parser v0.1.0         rmeta    0.014      0.038      0.024     s 0.001      s DAG root / initial
        Note: Unblocked by fresh dependencies (Cargo omits unblock events for fresh units)
   2    orchestrator v0.1.0          finish   0.038      0.063      0.025     s 0.000      s unit 5 (rmeta @ 0.038s) (direct)
   3    cli_app v0.1.0 (cli_app:bi   finish   0.063      0.141      0.078     s 0.000      s unit 4 (finish @ 0.063s) (synthetic/indirect)
```

Notice:
- Step 1 to Step 2 uses `rmeta` at `0.038s`: `orchestrator` started compiling as soon as `string_parser` produced metadata, while `string_parser` continued codegen until `0.051s`.
- `string_parser` unblocked `cli_app` in Cargo's queue via synthetic linking dependencies, but `orchestrator` was the final requirement satisfied.

## What the logs don't tell us yet

Working through the implementation and logs revealed a few places where the data does not provide full answers:

1. **Fresh units emit no unblocking events**: When a unit is up-to-date (cached), Cargo processes it on the main thread and skips timing event emission. If a rebuilding crate depends on cached crates, it has no unblocker recorded in the log. The analyzer knows it had fresh dependencies, but the exact timestamp when it became ready cannot be observed.
2. **Ready-to-start delays cannot be attributed**: When a crate becomes ready at $t_1$ and starts at $t_2$, the delay $t_2 - t_1$ is measurable. However, Cargo does not log jobserver token acquisitions or queue state. We cannot distinguish whether the crate was waiting for an available CPU core / job token, delayed by OS process spawning, or held behind higher-priority crates in the queue.
3. **Synthetic scheduling edges are unlogged**: When compiling linkable outputs, Cargo synthesizes full-compilation dependencies on transitive crates. These appear in `unblocked` lists, but the edges themselves are not registered in `unit-registered`, requiring post-hoc inference.
4. **Non-specific dirty causes**: If a unit reports `nothing-obvious` or `stale` without details, and multiple dependencies rebuilt, log data cannot prove which dependency caused the invalidation.

## Validation

The repository includes a validation harness (`scripts/run_validation_scenarios.py`) that runs 6 distinct scenarios against an isolated copy of `test_workspace/`:

1. Clean build (all units new)
2. No-change rebuild (all units fresh)
3. Leaf modification (`leaf_tool` modified)
4. Shared dependency modification (`string_parser` modified)
5. Serialized clean build (`-j 1`)
6. Parallel clean build (`-j 8`)

The harness cross-checks the analyzer's start times and durations against Cargo's internal HTML report generator (`cargo report timings -Zbuild-analysis`). In all executed scenarios, the reconstructed unit durations and start times match Cargo's internal numbers within 0.02s tolerance.

In addition, `tests/test_analyzer.py` contains 16 synthetic tests with manually derived expected values covering edge cases like broken pointers, inverted timestamps, metadata pipelining, and synthetic edges.

## Current status

This repository is an exploratory research prototype. Its main goal is to document what is already possible with Cargo's existing `-Zbuild-analysis` telemetry and identify the minimum missing instrumentation worth taking upstream to the Cargo team.
