# `cqa-report-v1`

`cqa-report-v1` is the presentation contract between Codex Quota Audit analysis code and its renderers.

It is intentionally **not** a dump of internal dataframes, CLI tables, or chart state. The analysis layer computes results; the report carries those results in a privacy-safe, renderer-neutral form; terminal/JSON/HTML consumers decide how to present them.

## Goals

The v1 contract is designed to:

- let the quota audit and workflow profiler feed one dashboard without duplicating analytical logic;
- keep the HTML renderer deterministic and simple (filter, sort, select, draw, navigate);
- support the dashboard's progressive disclosure model: overview -> analysis -> evidence;
- preserve enough evidence and uncertainty to audit every displayed conclusion;
- remain useful outside the dashboard (CLI, research scripts, tests, future applications);
- make the normal report safe to store or share without raw Codex conversation content.

## Non-goals

`cqa-report-v1` does **not** try to preserve every internal analyzer field. It is not a forensic export and it is not a browser-side analysis API.

The normal report must not contain:

- prompts or assistant responses;
- source-code or file contents;
- raw tool output;
- access/refresh tokens or authentication material;
- account identity (email, name, account ID);
- absolute source/session paths;
- raw Codex session/thread UUIDs;
- HTML fragments, CSS, chart colors, pixel positions, tooltip markup, or other presentation state.

If a future forensic export needs rawer data, it should use a separate schema/profile instead of weakening `dashboard-safe-v1`.

## Design rules

### 1. Analysis values, not formatted strings

Numbers stay numeric. For example, a derived quota efficiency is represented as roughly `39786703.31` tokens per quota point, not `"39.8M"`. The renderer chooses whether to show `39.8M`, `39,786,703`, or another human-readable form.

Shares use the `0..1` range (`0.989`, not `98.9`). Times are ISO-8601 timestamps. The production report builder normalizes timezone-aware analyzer timestamps to UTC (`Z`).

### 2. Derived values carry uncertainty when available

The common estimate shape is:

```json
{
  "estimate": 39.7867,
  "interval": {
    "method": "bootstrap_percentile",
    "level": 0.8,
    "low": 29.02,
    "high": 43.17
  }
}
```

`interval` may be `null` when the source analysis does not provide a meaningful interval. In sections where the underlying quantity itself is not identifiable (for example Guardian quota attribution or an unpriced banked-reset metric), the estimate object may also be `null` rather than using a fabricated zero. `level` may also be null when the source gives bounds but does not label them as a conventional confidence/credible level. When the analyzer exposes the number of bootstrap resamples, `interval.resamples` preserves that count; renderers may display it as evidence metadata but must not reinterpret the interval.

### 3. Report-local IDs are privacy-safe references

Relationships use IDs such as:

- `regime-001`
- `cohort-001`
- `guardian-period-001`
- `approval-001`
- `workflow-001`
- `agent-001`
- `compaction-001`
- `signal-001`
- `finding-001`

These IDs are unique inside a report. V1 does **not** promise that they remain stable across independently generated reports. Cross-report identity can be added later if a real UX need appears.

### 4. Sections always say why data is absent

Each major analytical section has a status:

- `complete`
- `partial`
- `not_requested`
- `not_available`
- `insufficient_data`
- `error`

This lets the UI distinguish "workflow analysis was not requested" from "workflow analysis ran but lacked enough evidence".

### 5. JavaScript does not perform statistics

The browser may filter, sort, select, format, and draw SVG. It must not reconstruct quota periods, detect policy regimes, fit Guardian overhead, bootstrap intervals, attribute workflow costs, or re-run any other substantive analysis.

If a number requires analytical judgment, Python should place the result in the report.

## Top-level shape

```text
cqa-report
├── generator
├── report
├── privacy
├── analysis
├── quota
├── guardian
├── banked_resets
├── workflow
├── findings[]
└── data_quality
```

The authoritative machine-readable definition is `schema/cqa-report-v1.schema.json`.

The production analyzer exposes the contract with `--report-json PATH`; `--dashboard [PATH]` renders the same in-memory report object into a self-contained HTML file. Legacy `--summary-json`, CSV, Markdown, PNG, and SVG outputs remain separate compatibility/publication formats.

## `generator`

Records the software that produced the report and optional component versions. A combined report can therefore record both the quota analyzer (`2.16` in the current package) and workflow profile source (`6.4`).

This provenance is deliberately separate from `schema_version`: analyzer releases may change without changing the report contract.

## `report`

Contains report-generation metadata and the overall observed time range. `kind` distinguishes an ordinary dashboard/export from a checked-in sample fixture.

Machine-specific source paths do not belong here.

## `privacy`

For v1 the standard profile is exactly `dashboard-safe-v1`. The schema requires every content-risk flag to be `false`.

This is intentionally redundant and testable: a renderer or CI test can reject a report that does not explicitly claim the safe profile.

## `analysis`

Stores method/version provenance and optional diagnostic parameters. It should contain enough information to explain why two reports generated from the same telemetry might differ after an analyzer-method change.

`analysis.parameters` is an escape hatch for diagnostic inputs; renderers should not depend on arbitrary entries there.

## `quota`

Quota analysis contains:

- a compact summary;
- model-specific detected policy regimes;
- model/effort/regime cohorts.

### Regimes

Regimes are model-specific because current analyzer output can assign the same display label (for example `R1`) to different model time spans. Cohorts therefore reference a concrete regime ID, not only a label.

### Cohorts

A cohort carries:

- model and reasoning effort;
- regime reference;
- quota-point/episode/bucket evidence;
- model and effort purity;
- raw-token efficiency and interval;
- optional public API-list-price-equivalent efficiency and interval;
- cached input, uncached input, and output composition per quota point.

API-dollar-equivalent values are normalization against public list prices. They are **not** subscription billing or OpenAI internal cost.

## `guardian`

Guardian is modeled at three levels:

1. summary;
2. reset periods;
3. paired approval episodes.

A period references its approval episodes by report-local IDs. This directly supports the dashboard drill-down: click a period -> inspect the estimate/interval -> expand the largest or all approval episodes.

Guardian quota overhead is observational. The report preserves the source estimate, bounds, rate-card information, and pairing metadata rather than presenting it as server billing truth. Workflow-only reports may also carry additive `extensions.pricing` evidence (coverage, rate-card provenance/as-of date, historical Auto-review mapping, long-context rules, per-model/rate-row priced totals, and long-context request/uplift evidence); this does not change the frozen v1 schema.

### Auto-review policy extension (0.9.1+)

The frozen v1 schema and statistical estimate fields are unchanged. `guardian.extensions.auto_review_quota_policy`, each Guardian period/episode's equivalent extension, and `workflow.profiles[].extensions.auto_review_quota_policy` carry additive policy evidence. Workflow `extensions.pricing.by_model[]` rows and `timeline.guardian_activity[].extensions` also preserve `auto_review_quota_policy` evidence.

The object includes `version`, `status` (`historical`, `free`, `transition`, `unknown`, `outside_scope`, `mixed`, or `none`), `announced_at`, `source_ref`, a scope/uncertainty `note`, `auth_declaration`, `auth_modes`, `auth_bases`, and `tokens_by_status` / `requests_by_status`. `policy_quota_points` is zero only for wholly eligible free activity; otherwise it is null. Optional `historical_estimate` contains a supported historical subtotal (`value`, `lo`, `hi`, an 80% coefficient-bootstrap range).

Free policy attribution is **not** stored as a statistical `estimated_quota_overhead` and has no fabricated interval. Mixed and unresolved periods leave that field null, with any historical subtotal in the extension. A wholly free Guardian section can be complete without a fitted estimate. Observed account meter movement and API-equivalent work remain unchanged. `quota.extensions.auto_review_policy_excluded_buckets` reports conservative evidence exclusions from quota-value fits. Renderers without these extensions must keep legacy reports readable without reclassifying their dates or authentication.

## `banked_resets`

Banked-reset data separates two questions:

- **whole-period comparisons**: banked periods vs same-model/effort/regime comparison periods;
- **boundary slices**: equal quota points immediately before vs after each confirmed banked reset.

Ratios are stored as estimates. `1.00` means equal observed capacity; `0.50` is the literal half-capacity reference. The renderer may draw those reference lines, but they are presentation semantics rather than chart coordinates.

The supplied machine-readable exports proved that two evidence layers are stable enough to make strict in v1:

### `capacity_periods`

This contains the matched whole reset periods that actually contribute to the aggregate capacity estimator. Each record preserves:

- whether the period is user-confirmed banked or a comparison period;
- reset-period start/end and quota points consumed;
- model, reasoning effort, detected policy regime, and model/effort token shares;
- all-work and Guardian-excluded token/API-equivalent totals and per-quota-point efficiency;
- Guardian token count;
- the supplied banked marker and match offset for confirmed banked periods.

The dashboard can therefore expand a whole-period aggregate into the exact banked/comparison periods behind it without reading the source CSV.

### `boundary_slices`

Each aggregate slice (for example 5, 10, 14, or 20 quota points) contains all four analyzer metrics:

- `all_raw`
- `all_api`
- `core_raw`
- `core_api`

Each metric has its own estimate and bootstrap interval when that metric is identifiable. API-normalized metrics may be `null` when price coverage is insufficient. `preferred_metric` records the analyzer-selected metric used for the headline view; the other metrics remain available for evidence inspection rather than being discarded.

### `boundary_observations`

This contains one privacy-safe record per confirmed reset × slice size. Each observation preserves:

- reset time and user-supplied marker precision;
- slice size;
- model/effort/regime control checks;
- before/after period starts and quota-meter ranges;
- whether either boundary had to be interpolated;
- equal-quota before/after token totals, Guardian-excluded totals, Guardian tokens, and API-list-equivalent values;
- model/effort purity on each side;
- all four per-reset after/before ratios.

This is deliberately narrower than the raw analyzer dataframe: cached/uncached/output component columns are not promoted merely because they exist. V1 includes only fields required to explain and audit the proposed banked-reset drill-down.

The aggregate interval methods used by the current analyzer are recorded as `whole_reset_period_bootstrap_percentile` and `whole_reset_pair_bootstrap_percentile`, with the interval level and resample count supplied by the report producer.

## `workflow`

Workflow analysis is optional. A normal quota-only report can set:

```json
{
  "status": "not_requested",
  "profiles": [],
  "extensions": {}
}
```

A profile contains:

- analysis window and headline totals;
- role-level additive usage;
- optional observed turn-throughput summaries for the whole workflow, each role, each model, and each privacy-safe agent;
- privacy-safe agent records with parent relationships and direct usage;
- compaction/recovery records;
- concurrency aggregates;
- a privacy-safe `timeline` made from trusted lifecycle windows and already-computed workflow evidence;
- observational investigation signals;
- explicitly reviewed pauses when available.

### Workflow turn throughput

`cqa-report-v1.0` permits optional throughput fields without changing the major schema version. Older v1 reports without them remain valid. When present, a workflow profile can contain:

- `throughput_method`: the exact definition/caveat used by the profiler;
- `throughput`: workflow-wide observed cadence summary;
- `throughput_by_model`: model-level summaries;
- `roles[].throughput`: role-level summaries;
- `agents[].throughput`: direct-agent summaries.

One **turn** is one deduplicated Codex `token_count` / `token_usage_record` usage sample carrying `last_token_usage`. Duplicate cumulative token-counter samples are removed by the workflow parser when cumulative totals are available. This is the same observed inference-request unit already counted as `requests` in the detailed workflow profile; `requests` is retained for backward compatibility.

The cadence metric is deliberately conservative. `turns_per_observed_second` is computed from adjacent observed turns in the same session stream whose completion timestamps are no more than the configured idle-gap threshold apart. For model-level cadence, both adjacent turns must have the same model. The aggregate rate is **weighted from totals** (`sum transitions / sum qualifying interval seconds`), never an arithmetic mean of per-agent rates.

`turns_per_workflow_second` uses the full workflow analysis window as its denominator. `tokens_per_turn` uses raw input+output tokens divided by observed turns. Optional `output_tokens` and `output_tokens_per_workflow_second` carry response-level model output attributed to that group and normalize it over the **full workflow analysis-window wall time**. The dashboard presents the latter as **Model output / elapsed min**.

These fields do **not** measure server-side inference latency, tokens/second decoding speed, or continuous model execution. Qualifying turn gaps can include tool execution, orchestration, scheduling, or other waiting time. The dashboard therefore labels the primary value **turn cadence** and displays it as turns/minute for readability. `raw_tokens_per_observed_second` is likewise an observed workflow work rate over qualifying cadence intervals, not a decoding-speed metric. `output_tokens_per_workflow_second` deliberately includes all intervening workflow time in its denominator and must be presented as an **end-to-end workflow output rate**, never as visible generation speed.

### Workflow model performance

`cqa-report-v1.0` also permits optional timing-qualified performance fields without changing the major schema version. Older v1 reports remain valid. When present, a workflow profile can contain:

- `performance_method`: the profiler's timing qualification/aggregation definitions;
- `performance`: workflow-wide timing summary;
- `performance_by_model`: model+effort timing summaries;
- `roles[].performance`: role-level timing summary;
- `agents[].performance`: direct-agent timing summary.

`visible_output_tokens_per_second` is response-scoped. It is populated only when the workflow profiler has response-level usage plus one or more positive-duration timed visible `AgentMessage` items and no model tool/function-call output in the same response. Tool-result records such as `function_call_output` are input to the following model response and must not be treated as model-generated call output. Visible tokens are `max(response_usage.output_tokens - response_usage.reasoning_output_tokens, 0)`, and multiple visible-message durations in one qualified response are summed. Aggregate tok/s is weighted as `sum qualified visible tokens / sum visible-generation seconds`. Cumulative task/turn usage is never divided by one visible-message interval.

Optional `visible_responses`, `visible_timed_responses`, `qualified_visible_responses`, `visible_timing_coverage`, and `visible_generation_coverage` fields let renderers distinguish whether missing tok/s evidence is caused by absent visible timing or by mixed visible+tool responses whose token budget cannot be split exactly. `generation_quality` may be `good`, `low`, or `insufficient`; quality is based on qualified visible responses / all visible responses.

TTFT is Codex-turn/task-level client timing and is attached to the first response in a task only. A tool-heavy Codex turn can contain many later model responses for which persisted rollout telemetry does not expose equivalent per-response TTFT. End-to-end task-duration distributions remain available as lifecycle context and should not be presented as model inference speed.

TTFT and end-to-end turn duration use client-observed `task_complete` timing when available and are summarized with P50/P90 plus explicit sample/coverage counts. These fields are not a server-internal stage decomposition. `qualification_reasons` preserves aggregate reasons that candidate turns did or did not qualify, without exposing the raw turn identifier or content.

### Workflow timeline

The timeline object exists so renderers do not need the original profiler JSON. It contains four privacy-safe collections:

- `agent_windows`: trusted child-agent lifecycle windows, remapped to report-local agent IDs;
- `guardian_activity`: Guardian/auto-review inference bursts with aggregate usage only;
- `concurrency_windows`: deterministic 2+ child overlap intervals derived from the trusted lifecycle windows;
- `handoffs`: profiler cycle transitions, preserving role pair, observed gap/overlap, and quality classification.

Compactions remain in `profile.compactions` and reviewed quiet/pause intervals remain in `profile.pauses`; the dashboard overlays those existing objects on the timeline rather than duplicating them. No prompt text, response text, tool output, source paths, or raw session/family identifiers are added for timeline rendering.

The report builder may normalize already-authoritative lifecycle geometry (for example, splitting active-window overlap into display-safe concurrency intervals). It must not infer new agent lifetimes, roles, attribution, pauses, or causal relationships.

The report deliberately omits raw request/event streams. The timeline carries only the compact lifecycle/activity evidence required by the dashboard, rather than the full request stream.

The production workflow profiler now emits this contract directly:

```bash
python3 -m cqa.workflow.profile \
  --family W-... \
  --report-json workflow-report.json \
  --dashboard
```

Its existing `--export-json` format remains the detailed privacy-safe workflow-profiler format. The shared report layer maps that already-computed export model into `workflow.profiles[]`; it does not redo attribution, compaction detection, concurrency analysis, pause review, or signal calculation.

A quota report can attach one or more detailed workflow exports at render/export time:

```bash
cqa audit \
  --dashboard \
  --workflow-profile-json workflow_cost_profile.json
```

During that translation the profiler's privacy-safe `W-`/`S-`/`Axx` identifiers are still remapped to report-local `workflow-...`, `agent-...`, `agent-window-...`, `compaction-...`, `handoff-...`, and `signal-...` IDs. The source family/session keys are not copied into `dashboard-safe-v1`. Agent `active` ranges are presentation spans over trusted lifecycle windows; the count/chunk coverage used to form that span remains in `extensions`.

Responsibility and topology remain separate in report presentation. Historical profiler aggregate buckets may encode the root position as a `/root` suffix; the report builder normalizes that suffix out of semantic role aggregation and records root position separately on the report-local agent (`extensions.is_root`). Renderers should show role (for example `coordinator` or `unknown`) and root position as separate facts, and must not infer a responsibility from root topology.

### Workflow signals

Signals such as post-compaction recovery, large-context/small-output share, **Extra concurrent agent-hours**, and lingering-agent candidates are labeled `observational`. Extra concurrent agent-hours is additive recognized child-agent time above one active child, so it can legitimately exceed workflow wall-clock duration. They are investigation prompts, not declarations of waste or causality.

## `findings`

`findings` are deterministic, analyzer-generated interpretations suitable for the dashboard overview. Examples include:

- visible quota cohorts span different policy regimes;
- Guardian overhead has a long-tail period;
- banked-reset boundary ratios approach equal capacity as slice size grows.

They are not LLM-written summaries. Each finding has evidence references so the UI can navigate from the statement to the underlying objects.

## `data_quality`

`data_quality` separates methodological caveats from analytical findings.

A finding says "something notable appears in the data." A quality warning says "this limits how you should interpret it."

V1 includes a few dashboard-relevant metrics:

- quota cohorts with/without API-equivalent pricing;
- Guardian pairing coverage;
- workflow session/attribution coverage;
- workflow lifetime-window status.

More fields should only be promoted here when they have stable semantics and a concrete renderer/user need.

## Extensions and schema evolution

Most objects are strict (`additionalProperties: false`) so accidental leakage or silent contract drift is caught early. Selected section objects include an `extensions` dictionary for experimental fields.

Experimental metrics should first live under `extensions`. Promote them to named v1/v2 fields only after their semantics and UX prove stable.

Workflow 0.7.0 uses this compatibility path for `performance_by_model[].extensions.response_efficiency` and `workflow.profiles[].extensions.response_efficiency`. These optional objects carry Tool-excluded output rate, task/tool/reasoning timing decomposition, TTFT, and evidence coverage. Older reports omit them; renderers must show the metric as unavailable rather than infer or synthesize it. The named v1 schema remains unchanged.

Versioning policy:

- **patch** (`1.0.1`): documentation/clarification or compatible schema corrections;
- **minor** (`1.1.0`): backwards-compatible optional fields/objects;
- **major** (`2.0.0`): incompatible changes to required fields or semantics.

Renderers should reject unknown major versions rather than guessing.

## Validation and privacy regression tests

The checked-in fixture should be validated in CI against the JSON Schema. A separate privacy lint should recursively reject suspicious keys/patterns such as raw prompts, auth fields, session UUID fields, and absolute Codex session paths.

A useful CI flow is:

```text
sample/current analysis
        |
        v
   build report
        |
        +--> JSON Schema validation
        +--> dashboard-safe privacy lint
        +--> critical metric regression assertions
        +--> HTML render smoke test
```


The sample fixture now exercises the banked-reset contract using the analyzer's actual `--summary-json`, `--export-banked-capacity`, and `--export-banked-slices` outputs, including bootstrap bounds and per-reset evidence rather than values parsed from SVG labels.

## V1 acceptance criterion and freeze

The policy-history and workflow-timeline milestone confirmed the contract can support the intended dashboard without reaching around it:

> The quota, Guardian, banked-reset, workflow, history, timeline, evidence, and drill-down UX can be rendered entirely from `cqa-report-v1`, without reading the original CSVs, PNG/SVG files, detailed workflow profile, or raw Codex logs.

`cqa-report-v1.0` is therefore treated as frozen from this milestone forward. New compatible fields should be optional or live under `extensions`; breaking structural or semantic changes require a new major schema version. Prototype reports generated before this freeze were development artifacts rather than a compatibility promise.
