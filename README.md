# Codex Quota Audit

**How much Codex work does your quota actually buy?**

`codex_quota_audit.py` analyzes your local Codex session logs to measure quota efficiency across models, reasoning-effort levels, and time.

It can help answer questions such as:

- Which model and reasoning-effort combination gives me the most work per 1% of quota?
- Did Codex quota generosity change over time?
- How much extra inference did **Approve for me / Guardian** create?
- How much of each 7-day allowance was plausibly spent on auto-review?
- Do user-confirmed **banked resets** provide less effective capacity than comparable reset periods?
- Does the same number of quota points buy less work immediately after a banked reset?
- Are replayed rollout histories or stale quota readings distorting a simple tokens-per-percent calculation?

The script reads local Codex JSONL telemetry under `~/.codex`, reconstructs effective quota accounting periods, filters replayed history, and relates observed token usage to the Codex rate-limit meter.

**Nothing leaves your machine.**

> [!IMPORTANT]
> This is an empirical analysis of local telemetry. It is not documentation of OpenAI's internal quota formula, billing system, or compute costs.

---

## Quick start

For most users, the recommended command is:

```bash
python3 codex_quota_audit.py --charts
```

This prints the highest-value findings and creates publication-ready PNG/SVG charts.

### Install chart support

Text analysis uses only the Python standard library. Charts require `matplotlib`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install matplotlib

python3 codex_quota_audit.py --charts
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install matplotlib

python codex_quota_audit.py --charts
```

If you do not want to install `matplotlib`, run:

```bash
python3 codex_quota_audit.py
```

---

## What you get

### 1. Model and reasoning-effort quota efficiency

The script compares sufficiently clean model/effort combinations using:

- **Mtok/1%**: million observed tokens per 1% quota
- **API$eq/1%**: public API-list-price-equivalent work per 1% quota

Higher values mean more observed work for the same quota movement.

The default comparison uses the **latest detected quota-policy regime for each model** instead of mixing all historical usage together.

This helps answer questions such as:

> Does Sol high buy materially more work per quota point than Astra high?

or:

> Is the difference still present after normalizing for public API token prices?

---

### 2. Quota-policy changes over time

Quota behavior can change independently of model choice.

The script detects model-specific **policy regimes** so historical changes are not hidden inside one all-time average.

Use:

```bash
python3 codex_quota_audit.py --history
```

to add:

- monthly quota-efficiency trends
- detected policy-regime tables
- model-by-month results

This is useful when asking:

> Did Codex actually become more or less generous, or am I comparing different models or time periods?

---

### 3. Approve for me / Guardian overhead

If your logs contain `codex-auto-review` activity, the script detects and analyzes it separately from ordinary parent work.

It can report:

- number of auto-review approval episodes
- extra Guardian inference tokens
- cached vs uncached input
- Guardian share of local approval context
- public GPT-5.4 rate-card-equivalent work
- estimated quota overhead per 7-day reset period
- median and high-end approval overhead

For example, the report can estimate values such as:

```text
typical active reset period:          ~1.0 / 100 quota points
worst observed reset period:         ~10.4 / 100
share of consumed quota while active: ~4.2%
```

These quota estimates are observational and include uncertainty intervals. They are not server-provided billing data.

If no auto-review / Guardian inference exists in the analyzed logs, the script says so and skips Guardian-specific tables and charts.

---

### 4. Banked-reset effective-capacity audit

The script can test the claim that a **banked reset visually restores the meter to 100%, but that 100% buys only half as much actual work as a comparable allowance**.

Provide timestamps for resets you personally triggered:

```bash
python3 codex_quota_audit.py --charts \
  --banked-reset 2026-09-05T23:11 \
  --banked-reset 2026-09-10T08:23 \
  --banked-reset 2026-09-13T15:03
```

`--banked-reset` is repeatable.

Timestamps without a timezone are interpreted in the machine's local timezone. Hour-, minute-, or second-resolution timestamps are accepted and matched to nearby reset transitions.

#### Whole-reset-period comparison

The first banked-reset test compares confirmed banked periods with comparison periods that have the same:

- dominant model
- reasoning effort
- detected quota-policy regime

It reports capacity ratios for:

- raw tokens, all work
- API-list-equivalent work, all work
- raw tokens excluding Guardian
- API-list-equivalent work excluding Guardian

Interpretation:

```text
1.00x = same effective capacity
0.50x = literal half-capacity prediction
```

The comparison periods are **not assumed to be normal/scheduled resets**. They are simply periods that were not user-confirmed as banked resets.

#### Equal-quota before/after boundary slices

Version 2.15 also directly tests the immediate before/after behavior around each confirmed banked reset.

For each reset it compares:

> the final N quota points before the reset
>
> vs
>
> the first N quota points after the reset

By default it tests **5, 10, 14, and 20 quota-point slices**. This is useful for reproducing claims based on a fixed-size quota slice while checking whether the result is stable across other slice sizes.

The audit reports before/after ratios for:

- raw tokens
- API-list-equivalent work
- raw tokens excluding Guardian
- API-list-equivalent work excluding Guardian

The strongest default metric excludes `codex-auto-review`, so unusual Approve-for-me activity does not masquerade as a banked-reset capacity change.

A pattern such as:

```text
 5pt   ~0.50x
10pt   ~0.50x
14pt   ~0.50x
20pt   ~0.50x
```

would be evidence consistent with a persistent half-capacity effect.

A pattern near `1.00x` across slice sizes argues against that claim.

A result that starts low and rises toward `1.00x` could instead suggest a temporary post-reset effect.

To reproduce only a 14-point test:

```bash
python3 codex_quota_audit.py --charts \
  --banked-reset 2026-09-10T08:23 \
  --banked-slice-points 14
```

`--banked-slice-points` is repeatable for custom slice sizes.

> [!NOTE]
> If a slice boundary cuts through a multi-point quota-meter jump, the script allocates that bucket's work proportionally by quota points.

---

## Generated charts

Running:

```bash
python3 codex_quota_audit.py --charts
```

can generate:

```text
quota_value_by_model_effort.png
quota_value_by_model_effort.svg
quota_chart_data.csv

guardian_approval_overhead.png
guardian_approval_overhead.svg

guardian_quota_by_period.png
guardian_quota_by_period.svg

banked_reset_capacity.png
banked_reset_capacity.svg

banked_reset_boundary_slices.png
banked_reset_boundary_slices.svg
```

Guardian charts are only created when relevant Guardian data exists.

Banked-reset charts require user-confirmed `--banked-reset` timestamps and enough usable comparison data.

The default chart theme is a restrained light report style intended for GitHub/Reddit sharing.

Use dark mode with:

```bash
python3 codex_quota_audit.py --charts --chart-theme dark
```

If you commit generated images to the repo, you can embed them in this README:

```markdown
![Quota value by model and effort](quota_value_by_model_effort.png)
![Approve-for-me cost by reset period](guardian_quota_by_period.png)
![Banked-reset effective-capacity audit](banked_reset_capacity.png)
![Banked-reset boundary-slice audit](banked_reset_boundary_slices.png)
```

---

## Recommended commands

### Most users

```bash
python3 codex_quota_audit.py --charts
```

### Text-only analysis

```bash
python3 codex_quota_audit.py
```

### Historical trends and policy regimes

```bash
python3 codex_quota_audit.py --history
```

or with charts:

```bash
python3 codex_quota_audit.py --charts --history
```

### Detailed forensic diagnostics

```bash
python3 codex_quota_audit.py --diagnostics
```

This adds detailed reset/replay/telemetry and Guardian diagnostics.

`--verbose` is an alias for `--diagnostics`.

### Experimental token-weight analysis

```bash
python3 codex_quota_audit.py --weight-details
```

This shows the identifiability-aware cached/uncached/output token-weight fits.

### Publication-ready report and machine-readable summary

```bash
python3 codex_quota_audit.py --charts \
  --report codex_quota_report.md \
  --summary-json codex_quota_summary.json
```

The generated report and summary contain aggregate results rather than prompts or model responses.

### Show all options

```bash
python3 codex_quota_audit.py --help
```

### Run synthetic self-tests

```bash
python3 codex_quota_audit.py --self-test
```

### Show version

```bash
python3 codex_quota_audit.py --version
```

---

## Data source

By default the script reads all available JSONL files under:

```text
~/.codex/sessions/**/*.jsonl
~/.codex/archived_sessions/*.jsonl
```

There is no built-in "last N months" cutoff.

If six months of logs are present, six months are analyzed. If more history is present, that history is analyzed too.

Use a different Codex directory with:

```bash
python3 codex_quota_audit.py --home /path/to/.codex
```

The default analysis targets the 7-day (`10080` minute) Codex limit.

The parser also records other rate-limit windows when present, including historical 5-hour telemetry, for diagnostics and Guardian analysis.

---

## Why a simple tokens-per-percent calculation is not enough

Codex logs contain several behaviors that can badly distort naive calculations.

### High-water quota accounting

`used_percent` can briefly move backward because of stale or out-of-order telemetry.

For example:

```text
40 -> 39 -> 41
```

is treated as a high-water increase from `40` to `41`, not as multiple independent quota movements.

### Effective reset reconstruction

A changed `resets_at` value is not automatically treated as a new quota allowance.

The script distinguishes:

- scheduled/on-time resets
- early resets
- after-due resets
- ambiguous boundaries
- near-zero `resets_at` churn

Near-zero churn is merged rather than allowed to restart the accounting baseline.

### Replayed rollout history

Some resumed/forked rollout files rapidly reconstruct historical cumulative `total_token_usage`.

Without filtering, this can make hundreds of millions of historical tokens look like fresh work.

The script detects probable replay prefixes from their cumulative-token sequence and excludes them by default.

Dense activity without positive replay evidence remains included.

### Policy-regime detection

Quota generosity can change over time.

The script detects model-specific regime shifts and avoids blindly pooling incompatible historical periods.

### Model and effort purity

Model/effort comparisons only use buckets that are sufficiently dominated by one model and reasoning-effort state.

### Whole-episode uncertainty

Chart intervals and several statistical analyses resample whole accounting/reset episodes instead of pretending individual 1% meter changes are independent observations.

---

## Approve for me / Guardian methodology

Auto-review inference is identified from local session metadata and `codex-auto-review` activity.

The script:

1. detects Guardian/auto-review sessions
2. pairs Guardian activity back to its likely parent Codex session
3. groups related activity into approval episodes
4. measures Guardian token overhead
5. associates approval episodes with available quota telemetry
6. estimates per-reset-period quota overhead conservatively

The quota meter is account-global, so the script does **not** claim that every meter movement surrounding a Guardian event was caused solely by Guardian.

Where the data cannot support a clean inference, the script reports that limitation rather than manufacturing a precise attribution.

### Public rate-card equivalent

For comparison purposes, `codex-auto-review` is mapped to GPT-5.4 in the script's public rate-card-equivalent calculation.

`Guardian $eq` is:

- a comparison ruler
- not a Pro subscription charge
- not OpenAI's internal compute cost

The script can apply documented long-context multipliers when the local telemetry provides enough information.

---

## Banked-reset methodology

The logs can show that a reset happened early, but `used_percent + resets_at` alone cannot prove **why** it happened.

That is why the script does not automatically label early resets as banked resets.

Instead, users provide reset-history timestamps they personally know were banked resets:

```bash
--banked-reset YYYY-MM-DDTHH:MM
```

Hour-, minute-, and second-resolution ISO timestamps are accepted.

The script then performs two complementary tests:

1. **Whole-period effective capacity**: compares banked periods with same-model/effort/policy-regime comparison periods.
2. **Equal-quota boundary slices**: compares equal quota-point slices immediately before and after each confirmed banked reset.

The second test is particularly useful for checking whether an apparent post-reset penalty is immediate and temporary or persists through larger portions of the allowance.

Both tests also calculate metrics excluding `codex-auto-review` so Guardian overhead does not masquerade as a banked-reset effect.

---

## API price normalization

Public API list prices are used as a common normalization ruler across differently priced models.

They are **not**:

- your ChatGPT/Codex bill
- subscription value
- OpenAI's internal cost
- proof of the server's actual quota formula

You can override or extend the built-in price table:

```bash
python3 codex_quota_audit.py --prices prices.json
```

Accepted formats:

```json
{
  "gpt-x": [4.0, 0.4, 20.0]
}
```

or:

```json
{
  "gpt-x": {
    "input": 4.0,
    "cached": 0.4,
    "output": 20.0
  }
}
```

Values are dollars per 1 million tokens for uncached input, cached input, and output.

If a model has no configured price, raw-token analysis still works, but API-normalized results may be unavailable for observations that lack sufficient pricing coverage.

---

## Exports

### High-water quota buckets

```bash
python3 codex_quota_audit.py \
  --export-buckets quota_buckets.csv
```

### Reset ledger

```bash
python3 codex_quota_audit.py \
  --export-resets reset_ledger.csv
```

### Model/effort chart aggregates

```bash
python3 codex_quota_audit.py \
  --export-chart-data quota_chart_data.csv
```

### Guardian approval episodes

```bash
python3 codex_quota_audit.py \
  --export-approval-episodes approval_episodes.csv
```

### Guardian quota cost by reset period

```bash
python3 codex_quota_audit.py \
  --export-guardian-periods guardian_periods.csv
```

### Banked-reset whole-period capacity

```bash
python3 codex_quota_audit.py \
  --banked-reset 2026-09-10T08:23 \
  --export-banked-capacity banked_capacity.csv
```

### Banked-reset equal-quota boundary slices

```bash
python3 codex_quota_audit.py \
  --banked-reset 2026-09-10T08:23 \
  --export-banked-slices banked_boundary_slices.csv
```

CSV exports contain timestamps and detailed usage patterns. Review them before publishing if that information is sensitive.

---

## Advanced / diagnostic analysis

### Replay-filter sensitivity

With diagnostics enabled, the script compares normal replay-filtered results with the same parsed logs including probable replay prefixes.

This shows how badly replayed history would distort the result if it were counted as fresh work.

### Token-type quota weights

The experimental weight analysis compares candidate models such as:

- one weight for all tokens
- uncached vs cached input
- input vs output
- uncached input + cached input + output

Validation holds out whole reset episodes and bootstraps whole episodes.

If predictors are too collinear or coefficients are unstable, the script reports a fit as weak or not identifiable instead of presenting a precise-looking coefficient.

---

## Privacy

The script reads local Codex logs only.

It does not upload your session data and does not make network requests.

Normal console output does not print prompts or model responses.

The default source is displayed as:

```text
Source: ~/.codex
```

rather than expanding your home-directory username.

The generated Markdown report and summary JSON are designed around aggregate, privacy-safe results.

CSV exports can contain timestamps and detailed usage patterns, so review them before sharing.

---

## Important caveats

- This is observational analysis of local telemetry, not authoritative documentation of OpenAI's quota implementation.
- `used_percent` is low-resolution/quantized telemetry and can contain stale or backward readings.
- An early reset's cause is not identifiable unless the user independently confirms that it was a banked reset.
- A user-confirmed banked-reset comparison is still observational. Comparison periods are not guaranteed to be scheduled resets.
- Boundary-slice tests compare equal quota-point windows, but the workload inside those windows can still differ.
- Multi-point meter jumps require proportional allocation when a requested slice cuts through a bucket.
- The current incomplete reset period can differ from completed periods simply because it is incomplete.
- API-list-equivalent dollars are a normalization unit, not billing or internal cost.
- More tokens or more API-equivalent work does not imply better answer quality.
- Some model/effort combinations are omitted when there is not enough clean evidence.
- Automatic policy-regime detection is statistical.
- Guardian quota attribution is estimated from account-global telemetry and should be interpreted with its uncertainty interval.
- Replay detection is deliberately conservative. Dense activity without positive replay-sequence evidence remains included.

---

## Full CLI reference

The script exposes additional tuning controls for:

- replay detection
- reset reconstruction
- model/effort purity
- policy-regime detection
- Guardian/approval pairing
- Guardian quota attribution
- banked-reset matching and whole-period capacity comparison
- equal-quota banked-reset boundary slices
- token-weight fitting
- chart bootstrapping
- chart theme and output paths

Run:

```bash
python3 codex_quota_audit.py --help
```

for the complete list and current defaults.

---

## Version

Current version:

```text
2.15
```

Check locally with:

```bash
python3 codex_quota_audit.py --version
```

---

## Disclaimer

This project is an independent analysis tool for locally recorded Codex telemetry.

Results should be treated as empirical evidence from the available logs, not as authoritative documentation of Codex quota policy or OpenAI's internal accounting.
