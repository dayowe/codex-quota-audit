# Workflow analysis methodology

The workflow profiler is the authoritative source for multi-agent attribution, lifecycle, compaction, concurrency, observed workflow cadence, and timing-qualified model-performance metrics used by Codex Quota Audit. The shared report/dashboard layer only normalizes already-computed profiler results.

See the [usage guide](usage.md#select-and-interpret-workflows) for commands and the [quota methodology](quota-methodology.md) for meter accounting and price normalization.

- [Roles and attribution](#workflow-agnostic-role-attribution)
- [Session and subtree totals](#session-subtree-and-assignment-totals)
- [Assignment identity](#assignment-identity)
- [Lifecycle and concurrency](#lifecycle-evidence-and-concurrency)
- [Analysis windows](#analysis-windows-and-restarts)
- [Compactions and timeline encoding](#compaction-refill-windows-and-timeline-encoding)
- [Pause accounting](#quiet-intervals-and-pause-accounting)
- [Workflow cadence](#observed-turns-and-workflow-behavior)
- [Model performance and timing](#timing-qualified-model-performance)
- [Before/after comparisons](#beforeafter-comparison-snapshot)

## Workflow-agnostic role attribution

CQA does not assume a particular agent architecture. A workflow root is **not** called a coordinator, planner, orchestrator, or any other responsibility merely because of its graph position or because it spawned a particular child role. Role evidence remains explicit and auditable.

The profiler resolves roles from structured session/spawn evidence first. When local telemetry cannot establish the workflow root role, users can declare it explicitly:

```bash
cqa workflow profile WORKFLOW_ID --root-role coordinator
```

`--root-role` accepts any normalized role label; `coordinator` is only an example. A local `workflow-role-map-v1` can supply multiple explicit overrides without changing source logs:

```json
{
  "schema": "workflow-role-map-v1",
  "root_role": "coordinator",
  "sessions": [
    {"session": "S-privacy-safe-key-or-local-raw-id", "role": "specialist"}
  ]
}
```

Raw selectors in this map are local audit inputs only. Portable reports contain only report-local agent IDs, resolved role labels, and evidence labels such as `declared-root-role` / `declared-role-map`. Contradictory explicit evidence remains a conflict rather than being silently overwritten.

In `cqa-report-v1` and the dashboard, **root is topology, not a role suffix**. Legacy detailed-profiler buckets may still contain values such as `unknown/root` for compatibility, but report presentation aggregates them under the semantic role (`unknown`) and marks the report-local root agent separately.

Role evidence can come from exact supported fields in the session's own `session_meta` (`role`, `agent_role`, `agent_type`, `role_name`), the leaf of its own recorded `agent_path`, a trusted matched structured spawn label, or an explicit mapping. Ancestor names and message sender/recipient paths cannot establish the session's own role. Leaf naming is weaker evidence than an explicit declaration.

The default recognition vocabulary is `coordinator`, `orchestrator`, `planner`, `implementer`, and `validator`. It is a labeling aid, not a required architecture. `--roles` replaces the vocabulary recognized in available metadata; it does not assign roles or classify conversation prose. Guardian is identified separately from recorded auto-review model metadata.

Explicit assignment evidence takes precedence over weaker naming evidence, with disagreements retained in diagnostics. Conflicting explicit roles, parents, or assignments block unit attribution. In detailed exports, inspect `role`, `role_confidence`, `parent_source`, `assignment_source`, `role_diagnostics`, `issues`, and attribution coverage before drawing role/task conclusions.

## Session, subtree, and assignment totals

The generic profile works with solo sessions, unnamed workers, flat teams, and nested agent trees. Missing role evidence produces `unknown` without dropping tokens. Usage without trusted spawn/lifetime evidence remains counted, but no lifetime interval is invented. `workflow_analysis` exposes lifetime coverage and the sessions missing that evidence.

The detailed `nested_attribution` view distinguishes:

- **Direct usage**: the session's own observed requests. Direct session totals reconcile to the selected primary total and can be added.
- **Inclusive subtree usage**: a session plus its resolved descendants. Parent and child subtree totals overlap and must not be summed.
- **Unit buckets**: work supported by explicit run/chunk/group identity, with role breakdowns and an unattributed remainder. Buckets plus the remainder reconcile to primary usage. Named-group lead work stays with the group and is not distributed across chunks.
- **Parent activity**: each parent's own usage while a given number of immediate children are observed alive. This is time association, not proof of supervision cost.

Root-wide work stays unattributed to chunks unless assignment evidence supports it. Cached input is a subset of input; reasoning is a subset of output. Missing prices do not erase usage and are exposed through pricing coverage.

## Assignment identity

Task-level attribution is optional. A role name, nearby timing, or arbitrary prompt prose is insufficient to establish a chunk or assignment. For future runs, the parent can put a generated label into a spawn tool's `task_name` where supported:

```bash
python3 -c 'from cqa.workflow.attribution import encode_label; print(encode_label("research-01", "researcher", 1, run_id="run-a"))'
```

Generating a label does not record it in telemetry. A trusted match between the recorded spawn and child session is also required. Use `--roles` to recognize custom role names, and test the runtime's label/linkage emission on a small run before relying on it.

The supported convention is:

```text
si1_<UTF-8 hex run ID>_<c or g>_<UTF-8 hex unit ID>_<role>_<attempt>
```

`c` identifies a chunk; `g` identifies an explicitly named group. Lowercase hex preserves original ID case/punctuation without slug collisions. Run/unit IDs must be nonempty, at most 96 UTF-8 bytes each, and free of control characters. Label roles use configured lowercase ASCII letters only; attempts are positive integers without leading zeroes. The complete label is at most 512 characters; respect stricter tool limits.

Same-worker repair/revalidation retains the assignment; a fresh replacement increments the attempt. A recorded parent distinguishes the same role/attempt under different leads. Legacy colon labels remain readable but lack explicit run identity and are scoped to the selected family. Arbitrary slug names are not decoded into assignments. Reports replace raw run/unit labels with local references and hash session/parent keys.

### Optional assignment map

For an existing run, use `--assignment-map mapping.json` only when a saved handoff or other trusted evidence establishes identity. Use session keys from discovery/lifecycle output, and include custom role names in `--roles`:

```json
{
  "schema": "workflow-assignment-map-v1",
  "family": "W-YOUR_FAMILY",
  "assignments": [
    {"session": "S-root", "role": "coordinator"},
    {
      "session": "S-worker",
      "parent_session": "S-parent",
      "run_id": "run-a",
      "unit_id": "O-03",
      "scope": "chunk",
      "role": "implementer",
      "attempt": 1,
      "completed_at": "2026-09-20T12:00:00Z"
    }
  ]
}
```

The map is validated against the selected family; cross-family joins are rejected. A role-only entry is supported. `completed_at` must be an explicitly recorded assignment end, not the last observed request; later usage is exposure requiring interpretation, not automatically waste. One session reused for different assignments cannot be split by this map: duplicate entries are rejected and conflicting assignments are withheld from unit attribution. Mapping evidence remains distinct from observed spawn/metadata evidence.

## Lifecycle evidence and concurrency

Lifecycle matching separates **trusted**, **diagnostic**, and **unresolved** evidence. Trusted matches use exact action/result targets or a unique child starting inside the tight spawn/start window (2 seconds by default). Broad proximity and `single-active-agent` hints remain diagnostic. Only trusted actions create follow-up phases or role-targeted supervision totals; a caller cannot resolve itself as its own target.

Repeated representations of the same action are deduplicated. An exact configured role in a routing field can establish a role-level SEND target; an actual session-ID bridge is still required to establish the individual recipient. Associating nearby parent inference with an action remains a non-causal timing heuristic.

Trusted descendant lifetime windows begin at the spawn timestamp, avoiding inherited rollout history, and normally end at the observed session end. `--active-tail-seconds` adds optional grace (`--stage-tail-seconds` is an alias). Alive means an observed lifetime, not continuous inference or productive work.

Family-wide concurrency covers all descendants with trusted evidence, including unknown roles. The nested view counts each parent's immediate children. These are different topology levels. Active-role states and 0/1/2/3+ descendant counts describe the root's own work during overlap; individual window-associated root totals can overlap and must not be summed.

Reports expose agent-time, active wall time, overlap time, extra concurrency-hours, and peak simultaneous descendants. Zero observed concurrency is not proof that no other worker ran. `--stage-roles` provides a separate role-filtered activity view without changing primary totals, core concurrency, or cycle isolation.

Generic mode assumes no stage order. Role-pair cycles require `--cycle-roles FIRST SECOND` or the opt-in `staged` profile. Staged mode defaults to implementer → validator cycles and planner → implementer, implementer → validator, validator → implementer successor relations. Generic mode has no default successor map.

Supervision ratios are reported only for complete isolated sequential cycles. Overlapping/truncated cycles, another active descendant, or explicit chunk mismatches suppress them. Unavailable cycle summaries remain unavailable, not zero. Structured identity can distinguish same-chunk repair, same-chunk replacement, cross-chunk, and unclassified overlaps. These are descriptive associations; lingering-agent candidates and post-trigger inference are observed exposure, not achievable savings.

## Analysis windows and restarts

`--after` and `--before` require explicit timezone offsets; `--before` is exclusive. Full-family analysis includes the last observed event by advancing an inferred upper bound by one microsecond. Explicit boundaries retain their stated meaning.

With a cutoff, root selection uses structural ancestry and observed in-window activity. It does not prefer coordinator/orchestrator titles or treat an inherited earliest timestamp as activation. Ambiguity requires `--analysis-root` with a session key or exact local ID (`--orchestrator` is a compatibility alias).

Sessions are classified as pre-window, carry-in, in-window, post-window, or carry-out. Trusted spawn time overrides inherited history when establishing child activation. Non-root carry-in workers are reported separately and excluded from primary post-restart totals by default. The selected root is the carry-in exception: only its in-window events count.

The selected segment contains the root and descendants meeting the window rules. An explicit leaf root does not acquire siblings as a fallback. Usage/actions before trusted child activation are excluded from primary totals and recorded in `pre_activation_records_excluded`. This does not prove that arbitrary inherited history was identified when no reliable activation boundary exists.

For comparisons, fix the root, window, source membership, and analyzer semantics. Changed role evidence, boundaries, or lifetime coverage can alter a view without constituting workflow savings. Preserve historical exports and write reruns to new paths.

## Compaction refill windows and timeline encoding

CQA counts only explicit persisted compaction markers. When both pre- and first post-compaction context are available, the dashboard can show a measured shrink. When the pre-compaction context is unavailable, the event remains a **compaction marker** rather than being promoted to a measured shrink episode.

**Context refill time** is the elapsed post-compaction observation window. It ends at the earliest of the configured refill threshold, the next compaction in that session, or session/analysis end. The default refill threshold is 80% of pre-compaction input and can be changed with `--compaction-refill-fraction`. The interval is not the duration of the compaction operation, and work observed inside it is not automatically causal overhead.

The workflow timeline intentionally mixes two visual primitives:

- **point events**: compactions and handoffs; horizontal position is meaningful, marker width is only for visibility;
- **elapsed-time intervals**: Guardian activity bursts, trusted agent lifetimes, concurrency windows, and reviewed pauses; segment width represents elapsed wall time subject to a minimum visible width for very short intervals.

Guardian activity bursts are first-to-last-request windows separated by the configured burst-gap threshold. A Guardian band therefore does not imply continuous inference for every millisecond inside the interval.

### Direct cost, context drops, and recovery observations

Nearby persisted `compacted` / `context_compacted` representations are deduplicated. The older context-growth `resets` field is a separate heuristic: a large request followed by input below 55% of its previous size. Explicit compactions and heuristic drops are compared, not treated as interchangeable evidence.

Direct compaction usage is reported only when raw usage can be safely matched near a marker. No nearby sample means unobserved direct cost; the next ordinary response is not assigned to compaction. A sample can exist even when cumulative usage does not advance, and therefore lie outside deduplicated primary totals. Inspect `direct_compaction_api_eq_outside_primary_totals` before adding direct compaction cost to a workflow total.

Recovery observations include input before/after, measured shrink where available, requests/tokens/price-normalized work, peak context, refill time, tool activity, and repeated resource access. The same-session non-recovery baseline compares work/input/tool events per request. A delta is exploratory; it does not establish compaction-caused overhead or savings.

Path-like resources are hashed locally; raw paths and commands are not exported. Default resource lookback is 20 minutes. Relevant controls are `--compaction-resource-lookback-minutes`, `--compaction-direct-usage-seconds` (default 1), `--compaction-dedupe-seconds` (default 2), `--compaction-limit`, and `--no-compaction-audit`.

The tool-activity view reports structural categories, duplicate removal, parse errors, and opaque/unclassified counts. JavaScript `functions.exec` wrappers remain opaque; code strings, comments, and branches do not prove execution. An interrupt does not imply close/released capacity, and tool counts cannot establish successful tests, unnecessary polling, or savings. No tool category receives a fabricated share of response tokens. Disabling compaction audit leaves this tool-activity view unavailable.

## Quiet intervals and pause accounting

Quiet candidates are gaps bounded by recorded calls across the whole analyzed family/subtree, at least an hour by default. A quiet root with busy workers does not qualify. Leading/trailing report silence is not suggested. Silence can reflect tests, external waiting, or missing telemetry, so normal profiling never automatically classifies it as a pause.

The report keeps the full elapsed-time rate and a separate hypothetical rate excluding unclassified gaps. The latter is a sensitivity check. Interactive [pause review](usage.md#review-pauses) saves explicit local decisions and can edit/add intervals inside the saved report window without rescanning logs.

Annotations follow the stable selected session root. A different explicit subtree root uses separate decisions. Changed gap evidence may produce new candidates; existing confirmed intervals still apply, and newly observed usage inside them is reported. Decisions outside the current window are retained. Normal profiling reads the store without creating a missing store.

Primary request/token totals remain intact. Pause-adjusted rates remove both time and any usage timestamped inside confirmed pauses. Intervals are start-inclusive/end-exclusive, merged to count overlap once, and clipped to the analysis window. Inferred gaps begin just after the preceding call and end at the following call, preserving both bounding requests. A pause covering the entire window makes the adjusted rate unavailable.

Adjusted time is not active CPU time or productive time. Calls are assigned by recorded timestamp; a response can cross a boundary. Concurrency, compaction, and existing comparison fields retain their original semantics. Review updates `pause_analysis` in an optional new copy, keeps the source snapshot unchanged, and rejects conflicting concurrent saves.

## Observed turns and workflow behavior

A **turn** is one deduplicated workflow `UsageRequest`, produced from Codex `token_count` / compatible usage telemetry carrying `last_token_usage`. When cumulative token totals are available, unchanged cumulative samples are discarded before profiling, so the turn count follows the same request unit already used throughout the profiler.

Turn cadence is an **observed workflow-behavior** metric, not model serving performance:

- adjacent turns are considered part of the same cadence burst only when their completion timestamps are separated by at most `--burst-gap-seconds` (120 seconds by default);
- role and overall cadence use adjacent turns within the same session;
- model/model-effort cadence additionally requires the model (and effort, where applicable) to be unchanged across the adjacent pair;
- aggregate cadence is `sum(observed transitions) / sum(qualifying gap seconds)`, so contributors are weighted by observed interval time rather than each agent receiving equal weight;
- `turns_per_workflow_second` divides all turns in that group by the complete workflow analysis-window duration;
- `tokens_per_turn` is raw input+output tokens divided by observed turns;
- `raw_tokens_per_observed_second` uses only raw tokens from turns that complete qualifying cadence intervals divided by those qualifying interval seconds;
- `output_tokens_per_workflow_second` divides all response-level model output tokens attributed to the group by the **full workflow analysis-window wall time**. It intentionally includes reasoning/tool-call output in the numerator and tools, waiting, orchestration, concurrency, and inactivity in the denominator.

A session with only one observed turn, or with no qualifying adjacent pair inside the idle-gap threshold, has no observed-cadence/work-rate value. Its turns and tokens still contribute to workflow-normalized pace and tokens/turn.

Turn cadence, observed work rate, and workflow-normalized output rate answer workflow-behavior questions, not serving-speed questions. They can include tool execution, orchestration, scheduling, and waiting. `output_tokens_per_workflow_second` is shown as **workflow output/min** and must not be described as model generation tokens/sec.

## Timing-qualified model performance

Modern Codex rollout telemetry can contain three independent timing/token scopes that must not be conflated:

1. `item_completed` events with `started_at_ms` / `completed_at_ms` for visible `AgentMessage` and `Reasoning` items inside a model response;
2. `token_usage_record.usage` with **response-level** input/output/reasoning token counts (distinct from cumulative `turn_token_usage`);
3. `task_complete` with **Codex-turn/task-level** `duration_ms` and `time_to_first_token_ms`.

Response-level generation metrics use (1)+(2). Turn responsiveness uses (3). The profiler never divides cumulative task/turn token usage by one visible-message interval.

The profiler retains only content-free timing/token aggregates. Raw turn IDs are one-way hashed before they can enter intermediate privacy-safe objects and are not exported in `cqa-report-v1`.

### Visible output tokens/sec

Visible-generation timing is **response-scoped**, not whole-task scoped. Modern Codex `token_usage_record` events may carry both response-level `usage` and task/turn-level cumulative usage. CQA uses the response-level usage only; cumulative turn usage is never divided by one short visible-message interval.

For a model response to contribute to **visible output tokens/sec**, it must have:

- response-level `usage` token counts;
- one or more positive-duration timed visible `AgentMessage` items emitted by that response;
- no model tool/function-call output in the same response.

Multiple visible `AgentMessage` items in one otherwise text-only response are valid: their durations are summed. Responses that mix visible text with tool/function-call output are excluded because response-level output tokens cover both and cannot be split defensibly. Missing/zero-duration visible timings and records that expose only cumulative turn-level usage are also excluded.

For a qualified response, visible output tokens are:

```text
max(response_usage.output_tokens - response_usage.reasoning_output_tokens, 0)
```

For an aggregate model/effort/role/agent cohort, visible-output generation throughput is weighted from totals:

```text
sum(visible output tokens from qualified responses)
----------------------------------------------------
sum(timed visible AgentMessage seconds)
```

It is never an arithmetic mean of per-response tok/s values.

CQA distinguishes two kinds of coverage:

- **visible timing coverage** = visible responses with positive-duration `AgentMessage` timing / all visible responses;
- **exact attribution coverage** = qualified text-only visible responses / all visible responses.

Tool-call-only model responses are not part of either denominator because there is no visible text generation to measure. Mixed visible+tool-call responses may have excellent visible timing but are excluded from tok/s because the response-level token budget cannot be split exactly.

Display quality is based on exact attribution coverage:

- **good**: at least 20 qualified visible responses and at least 50% exact attribution coverage;
- **low**: at least 5 qualified visible responses and at least 10% coverage, but below the good threshold;
- **insufficient**: fewer than 5 qualified responses or under 10% coverage.

The dashboard keeps a defensible numeric tok/s value visible even when the evidence class is **insufficient**, but labels it as a small-sample/directional estimate and does not present it as stable comparative evidence. A rate is shown as unavailable only when the report does not contain a qualified token/time rate. This is a client-observed visible-generation rate, not server-side instrumentation.

### Codex-turn responsiveness (TTFT and task duration)

When `task_complete` provides the fields, the profiler records one measurement per Codex task/turn:

- **turn TTFT**: turn start → the first model token of that Codex turn;
- **task duration**: end-to-end Codex turn/task duration.

A single Codex turn may contain many later model responses separated by tool calls. Current persisted rollout telemetry does **not** provide equivalent TTFT for each later response, so CQA must not present turn TTFT as per-response model latency.

Turn TTFT and task duration are summarized with P50/P90 and explicit sample counts. TTFT is not pure prompt-processing time, and task duration can include tools, subagents, orchestration, and waiting. Neither is a decomposition of internal server stages.

### Reasoning timing

The profiler may retain timed `Reasoning` duration as diagnostic evidence, but **reasoning tokens/sec is intentionally not published**. Its scope has not yet been validated across normal parent/worker response shapes. Reasoning tokens/turn remains descriptive and is not used to create a composite performance score.

## Presentation

The dashboard intentionally separates complementary questions and keeps established technical terms visible. Each canonical term is paired with a short plain-language explanation in the dashboard, while metric guides and this document provide the exact denominator, evidence, and qualification semantics.

### Model performance

- **Tool-excluded output rate**: non-reasoning model output / (`task_started` → `task_complete` elapsed time minus the union of exactly paired external tool-call → result spans);
- **Visible generation rate** in tokens/s is the complementary exact-attribution decoder-speed measure;
- **TTFT P50** is the median Time to First Token and **TTFT P90** is the 90th-percentile Time to First Token;
- **Reasoning share** is directly timed Reasoning divided by tool-excluded/retained response time;
- Exact / Partial / Unavailable evidence state comes from qualified-task and timing/pairing coverage.

The model drawer uses two denominator-explicit decompositions. First, total observed task time is split into **External tool / wait** (excluded) and **Retained response time**. Second, retained response time is split into directly timed Reasoning, timed visible generation, and **Other retained time**. The residual may include TTFT/request latency, model-resume/inter-item latency, tool-call generation/serialization, and small client overhead; it must not be relabeled as reasoning or server compute. Model/effort rows are observational workflow evidence, not controlled benchmark winners.

### Response timing

- **TTFT** = Time to First Token = task-start → first model token;
- **TTFT P50** = median first-token delay; **TTFT P90** = 90th-percentile first-token delay;
- **Task duration P50/P90** use the same percentile notation for end-to-end task duration;
- end-to-end task duration remains lifecycle context and may include tools/orchestration;
- explicit measured-task counts remain visible.

### Workflow pace

- **Observed turns/min** = qualifying adjacent turns per active minute (active response cadence);
- **Workflow-average turns/min** = observed turns / full workflow elapsed time;
- **Model output / elapsed min** = all response-level model output normalized by full workflow wall time;
- **Raw tokens/min · observed intervals** = raw token work / qualifying observed interval time;
- role/model/agent attribution remains explicit.

This prevents a high turns/min value from being misread as a faster model. Role/model/agent aggregates are computed in Python and carried through `cqa-report-v1`; browser JavaScript only presents the precomputed values.

## Before/after comparison snapshot

Detailed exports retain descriptive fields for comparing runs. Representative groups are:

| Question | Fields |
| --- | --- |
| Root work and context | `root_api_eq_share`, `root_requests`, `root_median_input_tokens`, `root_p90_input_tokens`, `large_context_small_output_api_eq_share` |
| Concurrency | `root_with_2plus_children_api_eq_share`, `recognized_child_agent_hours`, `extra_concurrency_hours`, `peak_concurrent_children` |
| Assignment coverage | `chunk_correlation_coverage`, `correlated_chunks`, `direct_child_requests_per_correlated_chunk`, `direct_child_api_eq_per_correlated_chunk` |
| Overlap exposure | `cross_chunk_overlap_api_eq`, `same_chunk_repair_overlap_api_eq`, `same_chunk_replacement_overlap_api_eq`, `unclassified_overlap_api_eq` |
| Direct compaction evidence | `explicit_compactions`, `direct_compaction_usage_coverage`, `direct_compaction_api_eq`, `direct_compaction_api_eq_outside_primary_totals` |
| Recovery work | `post_compaction_recovery_api_eq`, `post_compaction_recovery_api_eq_share`, `post_compaction_recovery_input_tokens`, `post_compaction_recovery_requests` |
| Root compaction / rereads | `root_compactions`, `root_compactions_per_hour`, `repeated_post_compaction_read_events`, `recovery_api_eq_delta_vs_same_session_baseline` |

These remain observations, not causal savings estimates. Overlap/exposure measures must not be summed into a savings number. Legacy `recognized_child_*` names cover descendants with trusted lifetime evidence regardless of role; the nested view separately reports immediate children.

## Validation corpus

The performance/response-efficiency rules are regression-tested against a sanitized structural derivative of a staged five-session Codex multi-agent workflow (root, orchestrator, two implementers, validator). The fixture preserves only event types, timestamps, response-level token counts, tool-call/output boundaries, and task timing. It contains **93 model responses**, **25 responses with visible messages**, **24 with usable visible-message timing**, and **9 text-only responses with exact token attribution**. Those 9 responses yield a weighted client-observed visible output rate of about **54.7 tok/s**.

The fixture intentionally preserves the real response-shape mix that exposed earlier bugs while removing prompts, assistant text, tool arguments/results, filesystem paths, and raw session/thread/response identifiers.

## Privacy

Performance aggregation requires timestamps, model/effort labels, role labels, and token counts only. The exported dashboard-safe report contains no prompt text, model response text, tool output, source paths, or raw session/thread/turn identifiers. Agent identities are remapped to report-local IDs.


## Pricing provenance

Workflow requests use the same request-level price normalization as the quota analyzer; see [price normalization](quota-methodology.md#price-normalization). Pricing evidence includes cost by observed model/resolved rate row, request/token totals, rates, priced/unpriced counts, long-context priced-request counts, and exact long-context uplift. The dashboard presents those Python-computed aggregates; browser JavaScript does not reconstruct prices. Partial coverage must not masquerade as a full estimate.

For historical schema and analyzer changes, inspect Git history. Package and internal analyzer versions are independent; use `cqa --version` to identify both.
