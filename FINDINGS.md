# Cargo build-analysis investigation

The useful result so far is narrower than the original critical-path claim: explicit fingerprint pointers can explain some rebuilds, and timing/unblocking events describe one observed chain into a selected target. They do not expose the whole resource-constrained schedule.

This is feedback for the [`-Zbuild-analysis` tracking issue](https://github.com/rust-lang/cargo/issues/15844), whose description links the original [build-analysis goal and discussion](https://github.com/rust-lang/goals/issues/332). No upstream changes are implemented here.

## Directly observed

### Evidence and toolchains

`validation_output/` contains six original JSONL sessions and their Cargo-generated timing HTML. Their logs record rustc **1.99.0**, commit `b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`. They do not record Cargo's own version or commit. The build commands identify a stable toolchain path, but that alone cannot prove which Cargo revision produced them.

During this audit, the installed Cargo was **1.99.0**, commit `5f94df4789f005f9a352888e8355ffc645b7ed0e`. I used that exact source revision as the baseline and compared the relevant implementation with upstream `master` resolved on 2026-10-23 to `07999b69c031c29852957e6d6eafc117c9310a94` (2026-10-22). This bounds the source comparison; it isn't a guarantee about future Cargo versions.

The original JSONL and HTML files were retained. Derived text, analysis JSON, and summary files were regenerated with the corrected analyzer. New six-scenario runs used isolated temporary workspaces and private `CARGO_HOME` directories; their summaries also capture `cargo -vV`. These additional runs were correctness checks, not a benchmark series.

### What the saved sessions contain

- Executed units have `unit-started`, sometimes `unit-rmeta-finished`, and `unit-finished` elapsed times. Completion events include the units unblocked by that transition.
- No-change builds contain registrations and fresh fingerprints but no unit execution events. There is no executed chain to time. This says nothing about total invocation time being zero.
- Dirty fingerprints can contain explicit unit pointers, or a local stale input with its path, reference artifact, stale-item category, and modification times. Modification times in these records are epoch **milliseconds**, not elapsed seconds.
- The shared-edit session records `cli_app → orchestrator → string_parser` through explicit fingerprint pointers, terminating at the edited source file.
- In that same session, metadata from `string_parser` unblocks `orchestrator`; full completion of `orchestrator` unblocks `cli_app`. Both relationships are registered direct dependencies. The earlier text report's synthetic label on the latter edge was wrong.
- In the saved `5b_clean_build_j8` session, `string_parser` instead supplies the final observed unblock for `cli_app`, despite being transitive in its registered graph. The final blocker can change across runs.

### Saved measurements, with the denominator made explicit

These numbers are derived from [`all_scenarios_summary.json`](validation_output/all_scenarios_summary.json). The span begins at the **first observed start on the selected chain** and ends at the selected target's finish. Measured gaps require an actual unblock event. The initial graph-to-start interval has unknown readiness and is excluded from both.

| Saved scenario | Executed units | Chain span (s) | Chain execution (s) | Measured gaps (s) | Initial interval, readiness unknown (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Initial clean build | 6 | 0.192846 | 0.192484 | 0.000361 | 0.001269 |
| No-change rebuild | 0 | N/A | N/A | N/A | N/A |
| Leaf edit | 1 | 0.057264 | 0.057264 | 0 | 0.000821 |
| Shared dependency edit | 4 | 0.127578 | 0.127120 | 0.000458 | 0.000846 |
| Clean `-j 1` | 6 | 0.491158 | 0.230687 | 0.260471 | 0.001415 |
| Clean `-j 8` | 6 | 0.223190 | 0.222729 | 0.000461 | 0.001110 |

The old `-j 1` total was 0.492573 s, with 0.261886 s labelled ready delay. Both included the 0.001415 s initial interval. The execution measurement, 0.230687 s, is unchanged. Under the corrected definition, measured gaps are **53.03% of that observed chain's span**. They are not a measured percentage of total Cargo wall time.

The harness compared 23 executed units with Cargo's timing HTML: all starts and durations agreed within 0.02 s. Zero units were compared for the no-change scenario, so its timing agreement is N/A. HTML and analyzer consume the same JSONL; this checks interpretation and rendering, not an independent clock or complete scheduling correctness. Cargo rounds to centiseconds; no timestamp tolerance is applied to raw JSONL consistency checks.

These tiny builds are noisy. The `-j` figures above are individual observations, not an estimate of speedup. Pipelining is observable, but there was no controlled non-pipelined baseline from which to measure its benefit.

## Verified against Cargo source

The following links are pinned to the audited Cargo 1.99.0 revision. I read the relevant producers, consumers, and queue/timing callers rather than deriving the rules from the fixture outputs.

| Source | Contract relevant to this analyzer |
| --- | --- |
| [`UnitInner::requires_upstream_objects`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/unit.rs#L149), [`TargetKind`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/workspace/manifest.rs#L275), [`CrateType`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/crate_type.rs#L70) | Test-like modes require objects. Library/example output requirements depend on their crate types; `lib`/`rlib` differ from other crate types. Non-build mode alone doesn't imply this property. |
| [`BuildRunner::only_requires_rmeta`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/build_runner/mod.rs#L765) | A metadata scheduler edge requires **both** units to be in Build mode and **neither** to require upstream objects. `Artifact::All` in check mode describes queue completion, not necessarily the production of object files. |
| [`JobQueue::enqueue`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L401) | Direct bin/test dependencies are filtered unless they are artifact dependencies or doc-scrape work. If the parent requires upstream objects, traversal inserts transitive `All` requirements. |
| [`depend_on_deps_of_deps`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L458) | The recursion traverses the original unit dependencies, including those filtered from the initial queue map. Inserting an existing key overwrites its artifact requirement with `All` and stops recursion at that key. An overlapping edge remains a registered direct relationship. |
| [`DependencyQueue::finish`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/util/dependency_queue.rs#L181), [`DrainState::finish`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L1186) | Finish removes a `(unit, artifact)` prerequisite. The returned units have no remaining prerequisites and are passed to the corresponding timing logger. An unblocking list identifies a final satisfied prerequisite, not every dependency completion. |
| [`unit_start`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/timings/mod.rs#L120), [`unit_rmeta_finished`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/timings/mod.rs#L152), [`unit_finished`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/timings/mod.rs#L178) | Start inserts the timed job into `Timings.active`; metadata completion uses `get`, full completion uses `remove`. Both completion methods return without logging if the job was not timed. The previous findings incorrectly described both as `remove`. |
| [`spawn_work_if_possible`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L557), [`Message::Run`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L618), [`run`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/job_queue/mod.rs#L989) | Dequeue, pending-queue insertion, token requests, dispatch, and receipt of a Run message are separate transitions. Fresh jobs execute synchronously and don't produce the timed Run transition. They still go through queue/dispatch handling; the earlier claim that they never encounter token gating was too strong. |
| [`UnitRegistered` population](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/ops/cargo_compile/mod.rs#L547), [`Target` conversion](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/util/log_message.rs#L181), [`CompileMode` serialization](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/build_config.rs#L189) | Indices cover the finalized unit graph; registrations preserve direct unit dependencies. Target conversion collapses all library crate types to `lib` and both example kinds to `example`. Mode serialization collapses check-test and ordinary check to `check`. Artifact-dependency flags are absent. |
| [`write_json_log`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/util/log_message.rs#L201) | Each serialized event has a run ID. Unit indices must not be merged across those session boundaries. |
| [`prepare_target`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/fingerprint/mod.rs#L459), [`DirtyReason`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/fingerprint/dirty_reason.rs#L17), [`FsStatus`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/fingerprint/mod.rs#L692), [`StaleItem`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/fingerprint/mod.rs#L862) | Fingerprint causes use flattened tagged objects. New builds and fresh units have distinct statuses. Stale items include file, checksum, metadata-read, size, and environment changes. A recognizable variant is not by itself proof of an ordinary root cause. |
| [`Fingerprint::compare`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/fingerprint/mod.rs#L1221) | A dependency pointer compares the parent's previous dependency fingerprint with the current one. It does not require that dependency to execute in this invocation. A pointer to a currently fresh dependency can therefore be valid while its earlier invalidation is outside this session. |
| [`cargo report timings`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/ops/cargo_report/timings.rs#L379), [`round_to_centisecond`](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/compiler/timings/report.rs#L567) | The renderer retains started units and rounds starts/durations to centiseconds. It is a useful comparison consumer, but not proof that changing existing finish-event semantics is safe for every consumer. |

### What changed after the baseline

At the compared [upstream revision](https://github.com/rust-lang/cargo/commit/07999b69c031c29852957e6d6eafc117c9310a94), the object-requirement predicate, metadata predicate, enqueue traversal/filter, readiness result, registration fields, and active-map timing guards have the same relevant behavior. `UnitRegistered` still loses the target/mode details above.

There are relevant additive/schema changes: [`UnitPeakMemory`](https://github.com/rust-lang/cargo/blob/07999b69c031c29852957e6d6eafc117c9310a94/src/util/log_message.rs) and its timing/report handling are new; [`checksum-use-changed` became `fingerprint-method-changed`](https://github.com/rust-lang/cargo/blob/07999b69c031c29852957e6d6eafc117c9310a94/src/compiler/fingerprint/dirty_reason.rs), with `old` and `new` methods instead of an old boolean. The analyzer ignores the unfamiliar memory event and preserves/classifies both fingerprint variants. This comparison does not claim that every internal fingerprint algorithm or artifact-layout behavior is unchanged.

### An additive event can still break an older report reader

I inserted a valid JSON event with an unfamiliar `reason` immediately after `build-started` in a copy of the saved clean-build log. Running installed Cargo 1.99.0's `cargo report timings` against that private log returned exit code 101 with `no timing data found in log`; the valid registrations/timings after the unfamiliar event weren't recovered. The [typed streaming deserializer](https://github.com/rust-lang/cargo/blob/5f94df4789f005f9a352888e8355ffc645b7ed0e/src/ops/cargo_report/timings.rs#L83) attempts to filter parse errors, but that does not establish that it can resume after an unknown enum variant. This test demonstrates a real compatibility concern for additional event kinds. It doesn't establish every reader's behavior. Our analyzer deliberately accepts unknown reasons; a Cargo proposal still needs a policy for older readers, and tests with the event in different stream positions.

## Inferred, with limits

### Scheduling requirements

The old predicate, `kind not in ("lib", "rlib") or mode != "build"`, wasn't equivalent to Cargo's. It also conflated object requirements with metadata eligibility. The inference now uses the logged target/mode constraints and a small `known` / `inferred` / `unknown` certainty value.

A bin-to-lib edge that survives queue filtering requires `All`. Test and doctest parents also require upstream objects. A check-mode edge cannot be a pipelined Metadata edge, but its parent's object requirement can remain unknown because the check-test flag was lost. Library-to-library Build requirements remain unknown unless other evidence narrows them.

An observed metadata unblock proves that both endpoints passed Cargo's metadata predicate. The analyzer can use that to narrow those units' object requirements and infer compatible metadata edges. Merely observing a unit produce rmeta is insufficient: other crate types can produce it too. This inference is explicitly based on observed events, not an independent validation of them.

Transitive candidates follow Cargo's insert-and-stop rule. When the parent requirement or initial edge filtering is unknown, the output retains uncertain candidates and a diagnostic. `required_artifact = all` with `certainty = unknown` on such a candidate means **if that candidate exists, the synthetic requirement is All**; it doesn't establish that the queue contains it. A known linking parent still receives transitive `All` requirements through a filtered binary; this analyzer conservatively leaves their certainty unknown while filtering is unresolved. The registered graph is retained separately, including repeated dependency references; the effective map consolidates keys as Cargo's queue does.

For each observed unblock, the report checks the downstream unit's own predecessor: registered direct first, confidently inferred synthetic second, unknown otherwise. Uncertainty about an artifact requirement doesn't erase a directly registered relationship. Classification never uses the following segment to label the preceding edge.

### Chain scope and incomplete data

Automatic target selection prefers the latest completed requested target. Without one, incomplete requested work is retained; otherwise the fallback is the latest finished or latest started unit. Explicit selection preserves an unstarted or fresh target instead of silently switching targets. The result reports the selection mode, unfinished requested work, and units finishing after the selected target.

A `complete` chain means its observed segments and measured gaps have consistent required timestamps. It does not establish complete Cargo execution or an observable readiness prefix. A fresh-only prefix is described as readiness unknown. A missing non-fresh predecessor or missing required phase completion makes the chain incomplete. Contradictory input makes it inconsistent. Totals are null for incomplete/inconsistent chains; known segment timestamps remain available. No-op sessions and explicitly fresh targets have no timed chain.

The first accepted record establishes either an identified session or an ID-free synthetic sequence. Identified sessions require matching IDs on every record, including unfamiliar events; an identified record after an ID-free prefix is also rejected, rather than adopting that prefix. Recovery mode excludes these records and preserves diagnostics; entirely ID-free fixtures remain supported. Duplicate/conflicting registrations, repeated completions/unblocks, invalid references/indices, non-finite numbers, graph cycles, and reversed timelines remain visible in final diagnostics. It never clamps them into plausible durations. Recoverable records arriving before registration retain their timing data when registration arrives later. Unknown future event types do not invalidate an otherwise valid input.

Forced rebuilds, generic dirty terminals, unknown reason variants, and broken pointers don't get a definite root index. A multi-unit explicit chain ending in an ambiguous terminal stays ambiguous. Detailed file metadata remains in JSON; text avoids printing the same source path twice.

## Unknown

Confirmed telemetry limits are the fresh readiness transitions, the lost target/mode/artifact fields, and the lack of timestamps separating scheduler/token transitions. Fingerprint records also lack check durations. Knowing those fields are absent doesn't establish how expensive the corresponding work is.

I couldn't establish token starvation, OS spawn cost, a complete resource-constrained critical path, whole-invocation completion time, or a quantitative pipelining benefit from these sessions. A chain can be measured accurately while omitting competing work that delayed it.

Other questions need a larger investigation: how much freshness processing matters in a large workspace; which missing scheduling fields external consumers really need; and which consumers depend on existing finish-event semantics. This six-crate experiment doesn't answer those.

## Potential upstream improvements

Ranked by the demonstrated problem, not by the amount of instrumentation we could add:

1. **Fresh readiness transitions: worth a focused discussion first.** The leaf/shared-edit sessions have executions with fresh prerequisites and no recorded readiness time. Existing registrations and fresh fingerprints identify dependencies but can't recover the missing instant. Documentation can explain the hole, not fill it. The smallest candidate is a dedicated readiness event only when fresh completion actually makes downstream work ready, carrying elapsed time, affected unit(s), and the satisfying artifact phase if it identifies the upstream completion. A dependency-ready event describes queue state; it should not pretend that a compiler process ran. A dedicated event keeps compilation-finish semantics intact, but is not automatically backward-compatible. An updated Cargo renderer could ignore it without adding compilation rows; older readers need explicit testing (see below). Consumers needing readiness would opt in. The cost would be an extra timestamp and serialized records for those transitions, with log volume proportional to emitted transitions. Neither overhead nor compatibility with all consumers has been measured. An upstream test should cache dependencies, edit a leaf, and verify one readiness transition without introducing a phantom compilation row; cover Metadata and All where applicable.

2. **Document registered versus scheduling edges before extending that schema.** A synthetic final unblock is demonstrated, and exact inference is blocked by missing crate types, check-test information, and artifact flags. Current `unblocked` events are already sufficient to follow the observed chain; the complete scheduler graph isn't required for that use case. Documentation of the unit graph, filter, and synthetic rule has no per-build logging cost or consumer schema impact and may be enough. If a consumer demonstrates a need for the full queue graph, logging actual `(dependency, artifact)` prerequisites at enqueue would avoid reimplementing private rules; log size would grow with that graph, especially synthesized edges, and must be measured. Adding only crate types wouldn't fix all the omissions. Test direct/overlapping/synthetic edges, filtered bin/test dependencies, artifact dependencies, and check/test modes upstream before specifying fields.

3. **Queue and jobserver timestamps: useful hypothesis, insufficient attribution evidence here.** The saved `-j 1` gaps are real; their causes aren't established. Existing starts, unblock times, and static job count cannot split dependency-ready time, pending-queue insertion, token request, token acquisition, dispatch, and Run-message handling. A single `queue_wait` number would still conflate several causes. Documentation should first state what `unit-started` measures. If maintainers need a particular distinction, add timestamps at those actual transitions, not an assumed per-unit token wait when tokens are requested/held through a shared helper. Costs include clock reads, records, and possibly correlating shared token state; they are unmeasured. Existing consumers could ignore additive events, but matching/correlation needs design. Upstream tests would need deterministic queue/token control and root-ready cases, not elapsed-time thresholds on tiny builds.

4. **Fingerprint-check timing: defer.** The schema doesn't time freshness checks, so cached-work attribution can't be reconstructed. These logs demonstrate absence of that measurement, not a performance problem. First measure a representative large no-change workspace or use existing profiling/tracing to establish that this cost matters. Only then consider aggregate phase timing or per-unit spans with a precisely defined scope; recursive/memoized fingerprint calculation could otherwise be double-counted. Per-unit spans add clock/log costs on the no-op path, while aggregate timing loses attribution. Consumer handling and tests would depend on the chosen scope. There is no evidence here to justify a fingerprint-timing PR yet.

A focused issue/comment about the first limitation is supported by a reproduction and source explanation. A Cargo patch still needs maintainer agreement on event meaning and compatibility. The repository is ready to share as an experiment with these limits, not as proof that all four additions are needed.

## Validation and test notes

Run `python3 -m unittest discover -s tests`. The tests deliberately separate source contracts from fabricated timing expectations:

| Tests | Independent basis and event sequence | Expected result |
| --- | --- | --- |
| Workspace final-unblocker regression | `enqueue` synthesizes All prerequisites; `finish` returns only final-ready transitions. Six real workspace relationships, with orchestrator or string_parser completing last. | Correct predecessor edge classified direct or inferred synthetic on the target segment. |
| Crate-type schema fixture | `tests/fixtures/library_registrations.jsonl` was captured with Cargo 1.99.0 from a temporary four-member workspace: `base`, and libraries with `[lib] crate-type = ["rlib"]`, `["cdylib"]`, `["staticlib"]`, each depending on base. Capture command: `cargo build -Zbuild-analysis` with logging enabled and bootstrap for the installed toolchain. Only registration records were retained; temporary workspace paths became `/workspace`, and envelope timestamps/run IDs were removed. | All three kinds are `lib`; the analyzer reports unknown requirements. No invented crate-type field enters the fixture. |
| Mode/overlap/filter tests | `only_requires_rmeta`, `is_any_test`, and `enqueue`; Build/Check/Test/Doctest registrations, overlapping direct/transitive prerequisites, a filtered binary with transitive libraries, and the doc-scrape exception. | Conditional All requirements retain unknown certainty where inference is conservative; registered edges and diagnostics survive, and no unblocker is inferred from registrations alone. |
| Metadata and converging branches | Explicit phase-ready transitions followed by starts before full upstream finish; competing branches finish in different orders. | Only the recorded final unblocker is followed; metadata contribution stops at rmeta. A time gap alone proves no jobserver cause. |
| Incomplete/invalid input | Remove a required finish, mix identified/ID-free records or different IDs, repeat records, invert raw timestamps, omit registrations, or create cycles. | Preserve known partial values, exclude foreign records, null unreliable totals, and reject invalid reconstruction in strict mode. Recovery and unknown future events are checked separately. |
| Fingerprint chains | Flattened DirtyReason/FsStatus/StaleItem schema and comparison code; explicit chains terminating in local, generic, forced, fresh, missing, or cyclic references. | Specific roots only where justified, terminal ambiguity retained, broken pointers reported, file details preserved. |
| JSON and harness boundaries | Serialize complete/partial/recovered data; compare saved same-session HTML; remove expected artifacts; simulate command failure or reuse a nonempty output directory. | Exact JSON round trips, no non-finite output, no zero-unit agreement percentage, and failures instead of stale success. |

The real harness checks exact status/execution counts, an acceptable chain status, timing-row indices and values, session-specific output existence, and original workspace hashes. Mutations occur only in the temporary copy. The fixed `-j 1` minimum-delay assertion was removed. Original source files and Cargo's upstream checkout were not changed by validation.

Manual checks cover text output, stdin, explicit target selection, JSON CLI output, the README analysis/validation commands, and the older-consumer experiment above. Temporary Cargo builds additionally exercised `cargo check` and `cargo test --no-run` with rlib/cdylib/staticlib manifests; these verify actual logged kinds/modes, not broad support for every build-script/artifact-dependency configuration. The rolling `cargo +nightly` command was not run: no rolling nightly is installed here. Live builds used installed Cargo 1.99.0 with bootstrap. No Windows/macOS run, controlled performance benchmark, or exhaustive Cargo configuration matrix was performed.
