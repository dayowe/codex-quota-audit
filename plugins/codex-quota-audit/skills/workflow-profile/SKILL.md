---
name: workflow-profile
description: Profile local Codex multi-agent workflow usage. Use when the user asks where a Codex workflow spent tokens or API-list-equivalent work, which agents or roles consumed work, model/role turn throughput, context growth or compaction recovery, concurrency, Guardian activity, handoffs, pauses, lingering-agent candidates, or a workflow timeline. Do not use for unrelated project-management workflows.
---
# Codex Workflow Profile

Use the bundled workflow profiler. It discovers and analyzes relationships from local Codex rollout telemetry; do not reconstruct the workflow manually from raw session text.

Run commands from this skill directory. Use `python3` on macOS/Linux and `python` on Windows if needed.

## Normal workflow dashboard

For the latest **multi-agent** workflow, always use the strict selector. Do not pass an output path for normal use; the CLI archives the report in the local CQA report library and maintains `latest/workflow.html` automatically:

```bash
python3 scripts/run.py workflow profile latest \
  --multi-agent-only \
  --quiet
```

This strict path excludes the currently running Codex session when Codex exposes `CODEX_THREAD_ID` / `CODEX_SESSION_ID`, requires at least one delegated non-Guardian worker with observed usage, and does **not** silently fall back to a standalone or Guardian-only session. If none is found, tell the user that no recent multi-agent workflow was found and offer `workflow candidates` instead.

For normal plugin use, do not invent a relative output path and do not bypass the bundled `cqa` launcher with internal profiler modules or deprecated compatibility scripts. Keep the dashboard under `~/.codex/codex-quota-audit/` unless the user explicitly asks for a different location.

For a workflow/session selector the user already supplied:

```bash
python3 scripts/run.py workflow profile W-EXAMPLE --quiet
```

If the user explicitly tells you the root session's responsibility, you may preserve that trusted fact with a **generic** role declaration:

```bash
python3 scripts/run.py workflow profile W-EXAMPLE --root-role coordinator --quiet
```

`coordinator` is only an example. Do not infer a root role from topology, child roles, skill names, prompt prose, or a particular workflow framework. `--root-role` accepts any normalized role label. If the user supplies a trusted local `workflow-role-map-v1`, pass it with `--role-map`; never invent one and never copy its raw session selectors into your answer.

Do not invent a selector. If the user wants to choose among candidates first, run:

```bash
python3 scripts/run.py workflow candidates
```

Candidate output uses privacy-safe workflow/session keys and aggregate metadata.

## Answering a specific workflow question

Generate the privacy-safe normalized report and inspect that instead of loading raw rollout contents into the conversation:

```bash
python3 scripts/run.py workflow profile latest \
  --multi-agent-only \
  --report-json \
  --no-open \
  --quiet
```

The workflow dashboard/report can show model timing (qualified visible-output tok/s, TTFT and turn duration when defensible timing telemetry exists), role and agent attribution, observed workflow cadence by role/model/agent, tokens per turn, compactions and post-compaction context refill windows, trusted agent lifetimes, Guardian activity bursts, concurrent-child windows, reviewed pauses, and profiler handoffs. Treat investigation signals as leads, not declarations of waste: overlapping agents, large-context/small-output requests, recovery work, or lingering candidates can all have legitimate causes.

When interpreting results:

- direct role/agent usage is additive; inclusive subtree totals can overlap and must not be summed as if independent;
- `API$eq` is a public API-list-price-equivalent normalization ruler, not the user's bill or quota consumption;
- missing linkage, ambiguous roles, or partial telemetry are limitations, not evidence of zero work;
- concurrency can trade more aggregate work for lower wall-clock time and is not inherently inefficient.
- turn cadence is an observed workflow-behavior metric derived from adjacent usage records and can include tool/orchestration waiting; it is not model generation speed;
- **Model output / elapsed min** is response-level model output divided by the full workflow analysis-window wall time. That denominator intentionally includes tools, orchestration, waits, subagents and scheduling; it is an end-to-end workflow-output rate, not generation speed;
- visible-output tok/s is response-scoped: it requires response-level usage plus timed visible AgentMessage output and excludes mixed tool/function-call responses; multiple visible messages in one text-only response are aggregated, and timing coverage/quality must remain visible.

## Privacy and execution boundaries

The normalized `cqa-report-v1` output is `dashboard-safe-v1`: no prompt/response text, tool output, source-code contents, auth/account identity, source paths, or raw Codex session IDs. Do not upload generated reports or detailed exports unless the user explicitly asks. Do not add network calls or remote telemetry.
