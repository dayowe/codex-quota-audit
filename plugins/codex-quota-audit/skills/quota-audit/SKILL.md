---
name: quota-audit
description: Build or inspect a local Codex quota-usage audit. Use when the user asks about quota efficiency, model or reasoning-effort usage, policy-regime changes, Guardian or Approve-for-me overhead, banked resets, quota history, or a Codex quota dashboard. Do not use for general token counting unrelated to Codex quota telemetry or for billing claims.
---
# Codex Quota Audit

Use the bundled local analyzer rather than estimating quota behavior yourself. It reads Codex rollout telemetry under the selected Codex home and renders a self-contained local HTML dashboard. It does not call a remote service and does not send session contents anywhere.

Run commands from this skill directory. Use `python3` on macOS/Linux and `python` on Windows if `python3` is only the Microsoft Store stub.

## Normal dashboard

```bash
python3 scripts/run.py dashboard --quiet
```

This archives the report under `~/.codex/codex-quota-audit/reports/`, updates the local report library and `latest/quota.html`, then opens the new report. The only normal stdout is the final local report path. Use `--no-open` when browser opening is undesirable.

If the user explicitly wants the latest multi-agent workflow included:

```bash
python3 scripts/run.py dashboard --workflow latest --workflow-multi-agent-only --quiet
```

If the user explicitly supplies a trusted root role for that attached workflow, preserve it generically with `--workflow-root-role ROLE`. Do not infer coordinator/planner/orchestrator from the workflow topology or from a particular plugin/skill convention. If the user supplies a trusted local `workflow-role-map-v1`, pass it with `--workflow-role-map`.

If the user supplies confirmed banked-reset timestamps, preserve them exactly and repeat the option:

```bash
python3 scripts/run.py dashboard \
  --banked-reset 2026-09-05T23:11 \
  --banked-reset 2026-09-10T08:23 \
  --quiet
```

Do not guess banked-reset timestamps.

## Answering a specific quantitative question

Generate the privacy-safe report contract and inspect that instead of reading raw rollout contents into the conversation:

```bash
python3 scripts/run.py dashboard --report-json --no-open --quiet
```

The report JSON uses the `dashboard-safe-v1` privacy profile. It contains aggregate analysis and report-local identifiers, not prompt/response text, tool output, source-code contents, auth/account identity, source paths, or raw Codex session IDs.

When quoting results, preserve these interpretation boundaries:

- quota-efficiency measurements are empirical observations from local telemetry, not OpenAI's internal quota formula;
- `API$eq` is a public API-list-price-equivalent normalization ruler, not a subscription bill or internal compute-cost estimate;
- Guardian quota attribution is observational and carries uncertainty;
- banked-reset comparisons apply only to user-confirmed reset timestamps and matched model/effort/policy evidence;
- policy regimes are detected from observed behavior and should remain attached to cross-model comparisons.

If the report has insufficient or partial evidence, say so rather than filling the gap from assumptions.

## Privacy and execution boundaries

Do not upload or share the generated report unless the user explicitly asks. Do not inspect `auth.json`. Do not print raw rollout lines. Do not add network calls, analytics, external fonts, or CDNs. The dashboard is designed to remain self-contained and local.

## Report library

If the user asks to browse or revisit previous reports, run:

```bash
python3 scripts/run.py reports
```

This opens the local report library. `reports list` provides a terminal listing. Do not ask the user to remember report IDs or labels.
