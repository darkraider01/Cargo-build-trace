# Cargo Build Trace

A small Python experiment around Cargo's unstable `-Zbuild-analysis` logs. I wanted to see how far the existing events could answer two questions:

- Why did Cargo rebuild this unit?
- Which work contributed to the selected target's completion time?

The rebuild pointers are useful. Reconstructing scheduling turned out to need more care: the registered unit graph, scheduler prerequisites, and observed unblocking events describe different things.

## Why I started looking into this

Cargo already records structured build metrics under [`-Zbuild-analysis`](https://github.com/rust-lang/cargo/issues/15844). The tracking issue links the original [build-analysis goal](https://github.com/rust-lang/goals/issues/332) and the ongoing discussion. Rather than start by asking for more instrumentation, I tried analyzing what Cargo already writes.

This repository contains the analyzer, a six-crate workspace, captured sessions, and a validation harness. [FINDINGS.md](FINDINGS.md) has the source audit, measurements, and remaining questions.

## What the prototype does

`build_trace.py` keeps four relationships separate:

- **Registered dependencies:** unit indices from `unit-registered`. This is Cargo's unit graph, including compilation modes and build-related units, rather than just manifest dependencies.
- **Inferred scheduling prerequisites:** artifact requirements and possible transitive linking edges. Each edge has a certainty value. These are not a complete reconstruction of Cargo's queue.
- **Observed unblocking events:** the predecessor that made the remaining scheduler prerequisites disappear, where Cargo logged that transition.
- **Fingerprint causes:** explicit dependency pointers and local invalidation reasons from `unit-fingerprint`.

Starting from a selected target, the analyzer follows recorded unblockers backward. It uses metadata completion when that event unblocked the next unit, and full completion otherwise. Each segment includes its execution interval and, when observed, its unblock-to-start gap.

This is an **observed dependency-unblocking chain**, not a full resource-constrained critical path. A delay can include other work that isn't on this chain. Missing events remain unknown; incomplete or inconsistent chains don't get a definitive total.

## Getting started

Python 3.9 or later is enough for analysis and tests. There are no third-party Python dependencies or installation steps. Generating new logs also needs a Cargo nightly with `-Zbuild-analysis` support; an older nightly may not have the current schema.

Enable logging and build your project:

```bash
CARGO_BUILD_ANALYSIS_ENABLED=true cargo +nightly build -Zbuild-analysis
```

Cargo writes sessions to `$CARGO_HOME/log/`, normally `~/.cargo/log/`. You can also enable `[build.analysis] enabled = true` in Cargo's config. The recorded validation sessions used Cargo/rustc 1.99.0 with `RUSTC_BOOTSTRAP=1`; that's a local test setup, not a requirement for analyzing those files.

From this repository, try the saved shared-dependency edit:

```bash
python3 build_trace.py validation_output/4_shared_dep_modification/4_shared_dep_modification_session.jsonl --strict

# Structured output, or select a particular session-local unit index
python3 build_trace.py validation_output/4_shared_dep_modification/4_shared_dep_modification_session.jsonl --format json
python3 build_trace.py validation_output/4_shared_dep_modification/4_shared_dep_modification_session.jsonl --target 0

# Stdin works too
cat validation_output/4_shared_dep_modification/4_shared_dep_modification_session.jsonl | python3 build_trace.py -

python3 -m unittest discover -s tests
```

For your own session, substitute its JSONL path. `--strict` rejects malformed records, mixed sessions, and invalid references or timelines. Without it, offending records are excluded where possible and diagnostics remain in text and JSON. Unknown future event types are ignored without invalidating the input. This is a reconstruction validator, not a complete implementation of Cargo's serde schema.

By default, selection prefers the latest completed requested target. If requested work is incomplete, the report says so; explicit `--target` also works for a fresh or unstarted unit. Selecting one target doesn't imply that all other Cargo work had finished.

Run the six real scenarios with:

```bash
# Choose a new output directory for each run.
python3 scripts/run_validation_scenarios.py --output-dir /tmp/cargo-trace-validation-run
```

The harness uses the `cargo` on `PATH` and sets `RUSTC_BOOTSTRAP=1` for the local experiment. It copies the workspace to a temporary directory and uses a private `CARGO_HOME`. A nonempty output directory is rejected so old artifacts can't pass for a new run. A failed scenario stops the run; `all_scenarios_summary.json` is written only after all six pass.

## A small example

In the saved `4_shared_dep_modification` session, editing `string_parser/src/lib.rs` caused this explicit fingerprint chain:

```text
cli_app (unit 0) -> orchestrator (unit 4) -> string_parser (unit 5)
```

The final reason includes the changed source path and reference-file metadata. No static-graph guess was needed to follow those pointers.

For `cli_app`, the observed completion chain is:

| Unit | Contribution ends at | Start | End | Relationship to its unblocker |
| --- | --- | ---: | ---: | --- |
| `string_parser` | metadata | 0.010858 s | 0.031544 s | none logged; dependencies were fresh |
| `orchestrator` | full finish | 0.031769 s | 0.058517 s | direct (`string_parser`) |
| `cli_app` | full finish | 0.058750 s | 0.138436 s | direct (`orchestrator`) |

`orchestrator` started before `string_parser` fully finished. The `orchestrator → cli_app` edge is direct. An earlier report incorrectly labelled it synthetic because the label was attached to the wrong chain segment.

A different final unblocker is possible. In the saved `5b_clean_build_j8` session, `string_parser` finished last among `cli_app`'s prerequisites and directly unblocked it in the queue. That relationship is inferred to be a synthetic linking edge: `cli_app` only lists `orchestrator` in its registered dependencies.

## What we found

- Explicit fingerprint pointers can lead to a specific changed file, but generic dirty reasons don't establish a root cause. Forced rebuilds have a known category without identifying an ordinary invalidating input.
- The logged `lib` kind loses crate types, including the difference between `rlib`, `cdylib`, and `staticlib`. The logged `check` mode loses its test flag. Exact scheduler inference isn't possible in every case.
- Cargo can add transitive full-artifact prerequisites for targets that need upstream objects. Its traversal also handles overlapping direct edges; it isn't simply an unrestricted transitive closure.
- Fresh units don't have the start/finish/unblock events used here. Their dependencies and fingerprint status remain visible, but their readiness transitions aren't timed.
- An unblock-to-start gap is measurable. Jobserver waiting, dispatch, priority, event-loop handling, and process overhead aren't separately attributable from these events.

The initial interval from unit-graph completion to the first chain start is reported separately with readiness unknown. It isn't included in measured ready gaps or the chain span. A no-change build has no executed chain; it still takes wall-clock time.

## Validation and limits

The six scenarios cover clean and no-change builds, leaf and shared-dependency edits, and clean builds with `-j 1` and `-j 8`. The harness compares each executed unit's start and duration against `cargo report timings -Zbuild-analysis --id <run_id>` for the same session. Both consume the same JSONL, so agreement checks our interpretation against Cargo's renderer; it isn't an independent measurement of compiler runtime or proof of the whole scheduling graph.

All 23 executed-unit comparisons in the saved sessions agree within 0.02 s. The no-change session compares zero units and reports N/A. Deterministic tests cover graph boundaries, different final unblockers, missing events, parser recovery, and broken fingerprint chains.

These builds last milliseconds. Individual `-j` observations aren't benchmarks, and observing pipelining doesn't measure its performance benefit. No controlled non-pipelined baseline was collected.

## Current status

This is an experimental analyzer intended to make a Cargo discussion more concrete. The supported output is useful with its uncertainty labels and diagnostics. It cannot yet answer everything that determined a build's completion time.

The clearest instrumentation question is whether Cargo should expose otherwise missing readiness transitions from fresh units. [FINDINGS.md](FINDINGS.md) weighs that against documentation and other possible additions. No Cargo patch is included here.
