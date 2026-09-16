# Codex Quota Audit

Analyze your local Codex logs to see how much work your quota buys across models, reasoning-effort levels, and time.

The script reads Codex `token_count` and rate-limit data from `~/.codex`, reconstructs effective quota resets, filters replayed rollout history, and relates observed token usage to changes in the 7-day quota meter.

It can also generate a model/effort chart showing:

- **Token throughput:** million observed tokens per 1% quota
- **API-list-equivalent value:** API-list-price-equivalent dollars per 1% quota

> API prices are only a normalization ruler so different models can be compared. They are **not** what your Codex plan bills and are not a claim about OpenAI's internal costs.

## Example chart

Running with `--charts` produces a chart like this:

```text
quota_value_by_model_effort.png
quota_value_by_model_effort.svg
quota_chart_data.csv
```

If you commit one of the generated images to the repo, you can show it here with:

```markdown
![Quota value by model and effort](quota_value_by_model_effort.png)
```

## Why this script is more careful than a simple tokens-per-percent calculation

Codex logs are messier than they initially look. The script accounts for several things that can otherwise distort the result:

- **Effective resets:** `resets_at` changes are not automatically treated as real quota resets. Near-zero timestamp churn is merged, while scheduled or material quota drops create new accounting episodes.
- **High-water accounting:** stale or backward `used_percent` readings do not create extra quota consumption. A sequence such as `40 -> 39 -> 41` counts as a high-water increase from 40 to 41, not two separate increases.
- **Replayed rollout history:** resumed/forked sessions can rapidly reconstruct large amounts of historical `total_token_usage`. These replay prefixes are detected from their cumulative-token sequence and excluded by default.
- **Policy-regime changes:** quota generosity can change over time. The script detects model-specific regime shifts rather than blindly averaging all historical usage together.
- **Reasoning effort:** model and effort (`low`, `medium`, `high`, `xhigh`, etc.) are tracked separately when available in the logs.
- **Identifiability checks:** token-type weight estimates are only reported when the data contain enough independent variation to support them.

## Requirements

### Text analysis

No third-party packages are required. A normal run uses only the Python standard library.

```bash
python3 codex_quota_audit.py
```

### Charts

Charts require `matplotlib`. A virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install matplotlib

python3 codex_quota_audit.py --charts
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Quick start

Download `codex_quota_audit.py`, then run:

```bash
python3 codex_quota_audit.py
```

By default it reads all available JSONL logs under:

```text
~/.codex/sessions/**/*.jsonl
~/.codex/archived_sessions/*.jsonl
```

There is no built-in "last N months" cutoff. If six months of logs are present, six months are analyzed; if more are present, those are analyzed too.

To see all commands and tuning options:

```bash
python3 codex_quota_audit.py --help
```

To check the version:

```bash
python3 codex_quota_audit.py --version
```

To run the built-in synthetic tests:

```bash
python3 codex_quota_audit.py --self-test
```

## Common commands

Run the full text analysis:

```bash
python3 codex_quota_audit.py
```

Generate chart data plus PNG/SVG charts:

```bash
python3 codex_quota_audit.py --charts
```

Plot all detected historical policy regimes instead of only the latest regime for each model:

```bash
python3 codex_quota_audit.py --charts --chart-all-regimes
```

Show detailed token-weight fit diagnostics:

```bash
python3 codex_quota_audit.py --weight-details
```

Export high-water quota buckets:

```bash
python3 codex_quota_audit.py --export-buckets quota_buckets.csv
```

Export the inferred reset ledger:

```bash
python3 codex_quota_audit.py --export-resets reset_ledger.csv
```

Export chart aggregates without rendering charts:

```bash
python3 codex_quota_audit.py --export-chart-data quota_chart_data.csv
```

Use a different Codex data directory:

```bash
python3 codex_quota_audit.py --home /path/to/.codex
```

## What the report contains

### Data audit

Shows how many files and token records were found, how many duplicate records were removed, how much probable replayed history was detected, and how much data remains in the primary analysis.

### Reset audit and reset ledger

Reconstructs accounting episodes from `used_percent` and `resets_at`.

Resets are classified as:

- **scheduled/on-time**
- **early**
- **after-due**
- **ambiguous**
- **no-op `resets_at` churn**

An early reset is observable from the timestamps, but the logs do not reveal whether it was a banked/user-triggered reset or a reset granted by OpenAI.

### Quota episodes

For each accounting episode the script reports the observed high-water quota movement, tokens, cache ratio, model mix, pricing coverage, and API-list-equivalent value per quota point where enough of the usage can be priced.

### Monthly trend

Aggregates high-water quota buckets by month to show how quota generosity changed over time.

### Model x month

Shows token throughput and API-list-equivalent value for sufficiently model-pure observations, without hiding time-dependent quota-policy changes inside one all-history model average.

### Token-type quota weights

Experimental fits compare several possible quota models:

- one weight for all tokens
- uncached vs cached input
- input vs output
- uncached input + cached input + output

Fits hold out whole reset episodes for validation and bootstrap whole episodes for uncertainty intervals. If the predictors are too collinear or the coefficients are unstable, the script reports the fit as weak or not identifiable rather than presenting a precise-looking number.

### Replay-filter sensitivity

Runs a comparison with probable replay prefixes included to show how much replayed historical usage would inflate the results if it were counted as fresh work.

## Charts

The default chart uses the **latest detected policy regime for each model**. This avoids mixing periods where quota accounting appears to have changed materially.

Each plotted model/effort combination must meet minimum model purity, effort purity, quota-point, and independent-episode thresholds.

Whiskers are generated by resampling **whole accounting episodes**, not individual turns or individual 1% meter changes.

The labels use:

- `pt` = observed quota percentage points supporting the estimate
- `ep` = independent accounting episodes supporting the estimate

Use:

```bash
python3 codex_quota_audit.py --charts --chart-all-regimes
```

if you want historical regimes shown separately.

## API price normalization

The script contains a price table used to translate token usage into an API-list-price-equivalent value. This makes workloads using differently priced models easier to compare.

You can override or extend the table with `--prices`.

Accepted JSON formats:

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

Values are dollars per 1 million tokens for uncached input, cached input, and output respectively.

If a model has no configured price, its raw usage can still be analyzed, but API-normalized metrics that lack sufficient priced-token coverage are omitted.

## Privacy

The script reads local Codex logs only. It does not upload your logs or make network requests.

Normal console output does not print prompts or model responses.

The default source path is displayed as:

```text
Source: ~/.codex
```

rather than expanding your home-directory username.

CSV exports contain timestamps and detailed usage patterns, so review them before sharing if that information is sensitive to you.

## Important caveats

- This is an empirical analysis of logged Codex behavior, not documentation of OpenAI's internal quota formula.
- `used_percent` is quantized and can contain stale/backward observations, which is why the analysis uses high-water accounting.
- API-list-equivalent dollars are a comparison unit only. They are not subscription value, billing, or internal cost.
- Quality is not measured. More tokens or more API-list-equivalent work does not necessarily mean a better answer.
- Some model/effort combinations may be omitted because there is not enough clean evidence.
- Automatic policy-regime detection is statistical. Use `--chart-all-regimes` and `--weight-details` when inspecting edge cases.
- Replay detection is deliberately conservative. Dense activity without positive replay-sequence evidence remains included.

## Advanced options

The script exposes tuning controls for replay detection, reset reconstruction, model/effort purity, policy-regime detection, token-weight fitting, and chart bootstrapping.

Run:

```bash
python3 codex_quota_audit.py --help
```

for the complete list and current defaults.

## Version

Current version:

```text
2.6
```
