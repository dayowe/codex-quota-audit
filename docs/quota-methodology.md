# Quota analysis methodology

Codex Quota Audit relates observed model usage in local Codex telemetry to movements of the rate-limit meter. It measures empirical quota efficiency; it does not recover OpenAI's internal quota formula or measure answer quality.

For commands, installation, and exports, see the [usage guide](usage.md). Workflow accounting and timing are covered separately in the [workflow methodology](workflow-profiler.md).

## Data and comparison units

The quota analyzer reads available JSONL logs under `<Codex home>/sessions/` and `<Codex home>/archived_sessions/`. Default Codex home is `~/.codex`; there is no built-in history cutoff. The default target is the 7-day (`10080` minute) limit. Other recorded windows, including historical 5-hour telemetry, can contribute diagnostics and Guardian evidence.

The main model/effort comparisons use:

- **Mtok/1%**: million observed input-plus-output tokens per quota point.
- **API$eq/1%**: public-rate-card-equivalent work per quota point, accounting for cached input and differently priced models.

Higher values mean more observed work for a given meter movement. They do not establish that the work produced better results. Cached input is a subset of input; reasoning tokens are a subset of output and are not added again to raw totals.

The default comparison uses the latest detected quota-policy regime for each model. Model/effort combinations with insufficient clean evidence can be omitted. Different models' latest regimes may cover different dates and workloads; this remains an observational comparison.

## Reconstructing quota accounting

### High-water meter movements

`used_percent` is coarse and can contain stale or out-of-order readings. Within an accounting episode, a sequence such as `40 → 39 → 41` contributes a high-water increase from 40 to 41, rather than counting the backward reading as another movement.

Usage is associated with these high-water quota buckets. A simple total-tokens / final-percent calculation can therefore differ materially from the audited result.

### Effective reset periods

A changed `resets_at` value does not automatically create another allowance. The analyzer distinguishes scheduled/on-time, early, after-due, and ambiguous reset boundaries. Near-zero reset-time churn is merged rather than allowed to restart the baseline repeatedly.

The current incomplete period can differ from completed periods simply because it is incomplete. Interpret its coverage and consumed quota before comparing capacity.

### Replay filtering

Resumed or forked rollouts can rapidly reconstruct historical cumulative `total_token_usage`, making old tokens look like new work. CQA looks for positive evidence of replay prefixes in the cumulative-token sequence and excludes probable prefixes by default.

Filtering is deliberately conservative: dense activity without that evidence remains included. The diagnostics can show how including probable replays changes the same analysis.

## Policy regimes, purity, and uncertainty

Model-specific regime detection looks for shifts in observed quota efficiency. It avoids pooling incompatible historical periods into one all-time average. The dashboard's History & regimes view exposes those windows and can filter the displayed cohort without recomputing the report.

Model/effort comparisons require buckets sufficiently dominated by one model and effort state. Purity and evidence thresholds protect attribution; they also mean some combinations have no defensible comparison.

Uncertainty intervals and several statistical analyses resample whole accounting/reset episodes. Individual one-point meter changes are not treated as independent observations. Regime detection is statistical, and interval estimates do not remove workload differences or limitations in the meter.

## Approve for me / Guardian

Guardian/auto-review inference is identified from local session metadata and `codex-auto-review` activity. The analyzer:

1. Identifies review sessions and likely parent sessions.
2. Groups related review activity into approval episodes.
3. Measures tokens, cached/uncached input, and price-normalized work.
4. Associates episodes with available quota evidence.
5. Applies the date/authentication policy and estimates historical quota overhead by reset period, with uncertainty and coverage information.

Reports can distinguish total extra review inference, its share of local approval context, and estimated allowance consumption. These are different quantities. The account-global quota meter can include other work around an approval event; nearby movement cannot be attributed solely to Guardian.

When no review inference is observed, Guardian tables and charts are skipped. Weak pairing or quota evidence remains a limitation rather than a precise-looking estimate. The bundled historical price mapping is date-aware; see [price normalization](#price-normalization).

### Auto-review's announced free quota policy

On October 6, 2026, Tibo Sottiaux announced that Auto-review is free for users signed in through a ChatGPT account and does not draw usage from their plan. The source is [the announcement](https://x.com/thsottiaux/status/2107368734981517634), published at **07:13:54.094 UTC**. CQA uses publication as a reporting reference, not as proof of the exact server activation time or retroactivity.

Review requests are classified individually:

| Activity | Quota attribution |
| --- | --- |
| Before October 6 UTC | Historical observational estimates, when supported by pairing and meter evidence. |
| Earlier October 6 UTC, before publication | Transition: unresolved. |
| At/after publication with ChatGPT sign-in evidence | **0 quota points under announced policy**; no statistical confidence interval. |
| At/after publication without sign-in evidence | Unknown; never silently assumed free. |
| Explicit API-key sign-in | Outside the announcement's scope; API billing is unspecified here. |

CQA reads only structured rollout `auth_mode` / `authentication_mode` metadata and recognized subscription `rate_limits.plan_type` values. Explicit authentication evidence takes precedence over a plan label. It does not read credentials or infer historical sign-in from your current account. `--auto-review-auth-mode chatgpt` or `api` fills missing evidence as a recorded local assumption; it cannot override explicit evidence or conflicting metadata.

Approval bursts split at policy and reconstructed reset boundaries. Thus a reported approval episode can be a segment of one burst. Reset periods retain one row and separate historical, free, transition, unknown and API token portions. Mixed periods may show a **historical estimated subtotal**; they do not present that subtotal as the full period's review cost. Historical coefficients use only pre-October 6 episodes whose parent context and quota snapshots also predate the transition day. Matched manual-approval comparisons use the same historical restriction.

The account-global meter remains observed evidence even during free reviews: concurrent work can move it. To prevent such movement from being attributed to free or unresolved review, quota-value fits and matched banked-reset comparisons conservatively exclude affected buckets/periods. This can reduce usable evidence. Raw review tokens, timing, timeline activity and nonzero API-list-equivalent work remain visible. Historical-only PNG/SVG quota charts retain their statistical intervals; the dashboard also displays free and unresolved activity.

Policy classification is applied when generating a report. Cached facts are reused when you change the declaration. Older saved reports keep their original results; regenerate them to apply this policy.

## User-confirmed banked resets

`used_percent` and `resets_at` can show that a reset happened early but cannot establish its cause. CQA labels a banked reset only when the user supplies a timestamp independently known to represent one.

Timestamps are matched to nearby reconstructed transitions. Hour-, minute-, and second-resolution timestamps are accepted; an omitted timezone uses the machine's local timezone.

### Whole-period effective capacity

Confirmed banked periods are compared with periods matched on dominant model, reasoning effort, and detected policy regime. The other periods are simply not user-confirmed as banked; they are not guaranteed to be scheduled resets.

Capacity ratios are calculated for raw tokens and price-normalized work, each with and without Guardian. A ratio of `1.00x` represents comparable observed capacity; `0.50x` is consistent with a literal half-capacity prediction. Neither establishes a causal effect by itself.

### Equal-quota boundary slices

For each confirmed reset, CQA compares the final N quota points before the reset with the first N points after it. Defaults are 5, 10, 14, and 20 points; custom sizes are repeatable.

If a slice boundary cuts through a multi-point meter jump, the bucket's work is allocated proportionally by quota points. This is an approximation imposed by the available telemetry.

Ratios are available for raw tokens and price-normalized work, including versions excluding `codex-auto-review`. Excluding Guardian helps keep unusually heavy review activity from masquerading as a capacity change.

A ratio near `0.50x` across several slice sizes is consistent with a persistent half-capacity effect; ratios near `1.00x` argue against that prediction. A low ratio that approaches `1.00x` in larger slices can suggest a temporary effect. Workload differences, price coverage, and quantized meter jumps can also change these patterns, so inspect individual reset evidence and uncertainty.

## Price normalization

**API$eq is a normalization ruler**, not a subscription charge, internal compute cost, or the server's quota formula. CQA applies a bundled public ChatGPT Work/Codex Standard token-rate table consistently to quota and workflow requests.

The report records rate-card provenance, an as-of date, priced/unpriced request counts, and token coverage. Inspect these rather than assuming the bundled table represents current prices. The implementation and its source references live in [the quota analyzer](../src/cqa/quota/audit.py).

The bundled rules include date-aware `codex-auto-review` mapping and request-level long-context adjustments for supported models, including explicit model exceptions. Fast-mode or regional multipliers are not inferred when the telemetry does not establish them. Reports expose rate rows and long-context uplift so the estimate can be inspected.

Missing prices do not erase tokens. Raw-token accounting continues, while price-normalized comparisons can be unavailable when coverage is insufficient. A partial priced subtotal must not be interpreted as a full-workflow estimate.

Use `--prices` to override or extend rates; the [usage guide](usage.md#price-overrides-and-diagnostics) documents accepted formats. Record overrides when comparing runs so a change of normalization table is not mistaken for a change of quota efficiency.

## Diagnostic and weight analysis

Replay sensitivity compares filtered results with probable replay prefixes included over the same parsed logs. This diagnoses counting artifacts; it does not prove that every remaining request was fresh work.

Experimental token-weight fits compare a single weight for all tokens with cached/uncached and input/output alternatives. Validation holds out whole reset episodes and bootstraps whole episodes. Collinear predictors or unstable coefficients are reported as weak or not identifiable rather than promoted into a precise internal quota formula.

For sharing, use the standard dashboard/report contract and review activity patterns. Detailed CSV exports can expose timestamps and usage patterns; see the [privacy model](privacy.md).
