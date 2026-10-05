# Workflow profiler methodology notes

The workflow profiler is the authoritative source for multi-agent attribution, lifecycle, compaction, concurrency, observed workflow cadence, and timing-qualified model-performance metrics used by Codex Quota Audit. The shared report/dashboard layer only normalizes already-computed profiler results.


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

## Compaction refill windows and timeline encoding

CQA counts only explicit persisted compaction markers. When both pre- and first post-compaction context are available, the dashboard can show a measured shrink. When the pre-compaction context is unavailable, the event remains a **compaction marker** rather than being promoted to a measured shrink episode.

**Context refill time** is the elapsed post-compaction observation window. It ends at the earliest of the configured refill threshold, the next compaction in that session, or session/analysis end. The default refill threshold is 80% of pre-compaction input and can be changed with `--compaction-refill-fraction`. The interval is not the duration of the compaction operation, and work observed inside it is not automatically causal overhead.

The workflow timeline intentionally mixes two visual primitives:

- **point events**: compactions and handoffs; horizontal position is meaningful, marker width is only for visibility;
- **elapsed-time intervals**: Guardian activity bursts, trusted agent lifetimes, concurrency windows, and reviewed pauses; segment width represents elapsed wall time subject to a minimum visible width for very short intervals.

Guardian activity bursts are first-to-last-request windows separated by the configured burst-gap threshold. A Guardian band therefore does not imply continuous inference for every millisecond inside the interval.

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

**Model performance**
- **Tool-excluded output rate**: non-reasoning model output / (`task_started` → `task_complete` elapsed time minus the union of exactly paired external tool-call → result spans);
- **Visible generation rate** in tokens/s is the complementary exact-attribution decoder-speed measure;
- **TTFT P50** is the median Time to First Token and **TTFT P90** is the 90th-percentile Time to First Token;
- **Reasoning share** is directly timed Reasoning divided by tool-excluded/retained response time;
- Exact / Partial / Unavailable evidence state comes from qualified-task and timing/pairing coverage.

The model drawer uses two denominator-explicit decompositions. First, total observed task time is split into **External tool / wait** (excluded) and **Retained response time**. Second, retained response time is split into directly timed Reasoning, timed visible generation, and **Other retained time**. The residual may include TTFT/request latency, model-resume/inter-item latency, tool-call generation/serialization, and small client overhead; it must not be relabeled as reasoning or server compute. Model/effort rows are observational workflow evidence, not controlled benchmark winners.

**Response timing**
- **TTFT** = Time to First Token = task-start → first model token;
- **TTFT P50** = median first-token delay; **TTFT P90** = 90th-percentile first-token delay;
- **Task duration P50/P90** use the same percentile notation for end-to-end task duration;
- end-to-end task duration remains lifecycle context and may include tools/orchestration;
- explicit measured-task counts remain visible.

**Workflow pace**
- **Observed turns/min** = qualifying adjacent turns per active minute (active response cadence);
- **Workflow-average turns/min** = observed turns / full workflow elapsed time;
- **Model output / elapsed min** = all response-level model output normalized by full workflow wall time;
- **Raw tokens/min · observed intervals** = raw token work / qualifying observed interval time;
- role/model/agent attribution remains explicit.

This prevents a high turns/min value from being misread as a faster model. Role/model/agent aggregates are computed in Python and carried through `cqa-report-v1`; browser JavaScript only presents the precomputed values.

## Validation corpus

The v6.7 performance/response-efficiency rules are regression-tested against a sanitized structural derivative of a staged five-session Codex multi-agent workflow (root, orchestrator, two implementers, validator). The fixture preserves only event types, timestamps, response-level token counts, tool-call/output boundaries, and task timing. It contains **93 model responses**, **25 responses with visible messages**, **24 with usable visible-message timing**, and **9 text-only responses with exact token attribution**. Those 9 responses yield a weighted client-observed visible output rate of about **54.7 tok/s**.

The fixture intentionally preserves the real response-shape mix that exposed earlier bugs while removing prompts, assistant text, tool arguments/results, filesystem paths, and raw session/thread/response identifiers.

## Privacy

Performance aggregation requires timestamps, model/effort labels, role labels, and token counts only. The exported dashboard-safe report contains no prompt text, model response text, tool output, source paths, or raw session/thread/turn identifiers. Agent identities are remapped to report-local IDs.


## v6.8 pricing normalization

v6.8 leaves workflow attribution, cadence, visible-generation, and response-efficiency semantics unchanged. It centralizes request-level ChatGPT Work/Codex Standard rate normalization with the quota analyzer: GPT-6.1 Sol is priced explicitly; `codex-auto-review` maps to GPT-5.4 before 2026-07-30 and GPT-5.6 Luna on/after that date; supported requests above 272K input tokens use the documented long-context multipliers except for the GPT-6 Astra Codex exemption. Pricing coverage and rate-card provenance are exported as additive evidence so a partial priced subtotal cannot masquerade as a full workflow estimate.

## v6.9 pricing provenance and CLI progress

v6.9 leaves the v6.8 pricing formulas unchanged and adds auditable aggregates under the existing free-form pricing extension: cost by observed model / resolved rate-card row, request/token totals, per-million rates, priced/unpriced request counts, long-context priced-request counts, and the exact uplift contributed by supported long-context multipliers. The dashboard uses these precomputed values for its pricing-details drawer; browser JavaScript does not reconstruct prices.

When invoked through `cqa workflow profile`, long scans emit concise stage/count progress by default. `--quiet` suppresses that chatter and `--show-analysis-output` exposes the profiler's detailed diagnostics instead. Percentages/ETAs are intentionally omitted unless a real denominator exists.
