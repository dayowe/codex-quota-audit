# Throughput comparison research harness

`cqa research throughput-compare` is a developer-facing, privacy-safe research utility for comparing three different throughput semantics over the **same local Codex rollout logs**. As of 0.7.0, the validated **Tool-excluded output rate** is also presented in the workflow model-performance dashboard; the research-only Tokscale-style diagnostics remain out of production presentation. Existing visible-generation tok/s and frozen `cqa-report-v1.0` semantics remain unchanged.

## Why this exists

CQA's dashboard metric, Tokscale/Token Monitor accounting intervals, and a tool-excluded model-side interval all divide model output by elapsed time, but they choose different evidence boundaries. The comparison harness was used to answer the empirical question before production promotion and remains available for validation/regression work:

> Are the tools exposing additional useful timing evidence, or are they simply measuring different parts of an agentic turn? Can reasoning/TTFT remain in a model-side rate while external tool execution/waiting is stopped out?

Run it locally against a workflow/session:

```bash
cqa research throughput-compare latest
cqa research throughput-compare W-YOUR-WORKFLOW
cqa research throughput-compare YOUR_RAW_SESSION_ID
```

By default it writes:

```text
~/.codex/codex-quota-audit/research/throughput-compare.json
~/.codex/codex-quota-audit/research/throughput-compare.csv
```

Use `--json PATH` and `--csv PATH` to choose explicit outputs. Either format can be disabled with `--no-json` or `--no-csv`.

## Exact-population upstream Tokscale staging

Date-window Tokscale runs can include unrelated Codex sessions from the same boundary dates. To validate the reconstruction against upstream Tokscale on exactly the same rollout population CQA selected, create a dedicated local staging home:

```bash
cqa research throughput-compare W-YOUR-WORKFLOW \
  --stage-tokscale-home /tmp/cqa-tokscale
```

CQA resolves the workflow/session once, builds the normal privacy-safe comparison artifacts, and creates `/tmp/cqa-tokscale/sessions/` containing **only** the selected rollout files. It materializes ordinary files as hard links when the source/destination filesystem permits and falls back to copies otherwise. This avoids requiring Tokscale to follow symlinks while normally avoiding duplicate disk usage. Treat a hard-linked stage as read-only because modifying a hard link modifies the original rollout file.

After staging, CQA prints the exact upstream command. The staged path is a Codex home (`sessions/...`), so upstream Tokscale must discover it through `CODEX_HOME`, not Tokscale's unrelated `--home` option. Equivalent manual form:

```bash
CODEX_HOME=/tmp/cqa-tokscale \
npx --yes tokscale@latest models \
  --client codex \
  --group-by model \
  --json > tokscale-exact.json
```

Do **not** add `--since`/`--until` for this validation run: the stage itself is the population filter. A content-free `.cqa-tokscale-stage.json` marker lets CQA safely rebuild a stage it owns. CQA refuses to replace a non-empty directory without that marker and refuses staging paths that overlap the real Codex home. Existing files beside an owned stage (for example `tokscale-exact.json`) are preserved when `sessions/` is rebuilt.

**Privacy warning:** unlike `throughput-compare.json`/`.csv`, the staging directory is not shareable. It contains raw rollout bytes and original rollout filenames, which may include raw session identifiers, prompts, model responses, tool content, file paths, or other local telemetry. Keep it local and delete it when validation is finished.

Staging exact files does not force upstream Tokscale to apply CQA's trusted child-activation filter; Tokscale remains responsible for its own fork/replay/deduplication behavior. That remaining semantic difference is intentional and is part of what exact-population validation is meant to reveal.

## CQA visible-generation side

The CQA column is the existing production calculation, unchanged:

```text
sum exactly attributable non-reasoning visible output tokens
-----------------------------------------------------------
sum positive-duration visible AgentMessage generation time
```

A response qualifies only when CQA has response-level usage, positive visible `AgentMessage` timing, and no model-generated tool/function-call output sharing that response token budget. Mixed visible+tool responses are excluded instead of guessed.

This is the same semantic used by the workflow dashboard's **visible tok/s** value.

## Tokscale-style side

The second column is explicitly named **Tokscale-style**. It is a local reconstruction based on the current Tokscale Codex parser semantics inspected for this release, not a claim that upstream Tokscale itself produced the number.

The reconstruction:

1. starts/resets a timing cursor at `turn_context`;
2. also uses a human `user_message` as a defensive reset so missing turn context does not bridge arbitrary inter-turn idle time;
3. accepts Codex `token_count` usage snapshots while rejecting duplicate cumulative totals, near-stale regressions and zero-token snapshots;
4. measures each accepted entry from the previous accepted timing cursor, so accepted durations do not overlap;
5. uses non-reasoning output tokens for the displayed output rate;
6. does not advance the timing cursor for rejected snapshots.

The resulting headline is:

```text
sum non-reasoning output tokens on timed accepted token_count entries
--------------------------------------------------------------------
sum non-overlapping accepted token_count interval duration
```

This is intentionally broader than CQA visible generation. The first accepted interval after a turn/user boundary can include startup/TTFT-like elapsed time. Later accounting intervals can contain tool execution, orchestration, waits or other client-side elapsed time if those events occur between accepted accounting checkpoints.

## Tool-excluded response side

The third research metric is designed for reasoning-effort comparisons where reasoning latency should count against responsiveness but external tools should not. It uses the observed Codex task interval and exact call/result pairing available in raw telemetry:

```text
tool-excluded response time
    =
sum observed task_started -> task_complete elapsed time
    -
union of paired model tool-call -> matching tool-result/output spans
```

The primary numerator is response-level **non-reasoning model output**:

```text
sum (output_tokens - reasoning_output_tokens)
-------------------------------------------------
sum qualified tool-excluded response time
```

Model-generated tool-call tokens remain in the numerator because they are model output produced before external execution begins. The external call->result wall-clock span is what is removed. This means higher reasoning effort can lower the rate even when visible decoding itself is fast, while a slow shell command, subagent wait, or other external tool execution does not directly dilute the model-side denominator.

A task qualifies for the headline only when CQA has an observed positive `task_started` -> `task_complete` interval, response-level usage, and complete valid pairing for every observed model tool call/result in that task. CQA does not guess unpaired tool duration. Evidence quality is `exact`, `partial`, or `unavailable` based on task qualification/pairing coverage.

Raw Codex telemetry also exposes timed `Reasoning` and `AgentMessage` items. The research output therefore decomposes qualified tool-excluded time into directly timed reasoning, directly timed visible generation, and **other tool-excluded time**. That residual can include TTFT/request latency, model-resume/inter-item latency, tool-call generation/serialization, and small client overhead. It must not be described as server-internal compute time.

For attribution context, an additional `exact_visible_output_tokens_per_second` value uses only exactly attributable visible text responses. It is intentionally a lower-bound diagnostic in agentic runs because visible commentary can share a response token budget with model tool calls and therefore cannot always be split exactly.

Current upstream references used while implementing the harness:

- Tokscale Codex parser: <https://github.com/junhoyeo/tokscale/blob/main/crates/tokscale-core/src/sessions/codex.rs>
- Tokscale PR #878, non-overlapping Codex token durations: <https://github.com/junhoyeo/tokscale/pull/878>
- Token Monitor token-rate presentation: <https://github.com/Javis603/token-monitor/blob/main/src/electron/renderer/tokenRatePresentation.js>
- Token Monitor API timing/throughput semantics: <https://github.com/Javis603/token-monitor/blob/main/docs/API.md>

## Diagnostic variants

The JSON/CSV output includes more than the two headline rates so disagreements can be explained rather than merely observed:

- **later-interval tok/s** excludes first intervals after `turn_context`/human user-message resets, helping isolate startup contribution;
- **excluding tool-signal intervals** drops intervals in which tool-related call/result telemetry occurred;
- **excluding tool-result intervals** is a narrower diagnostic that drops intervals containing result/output evidence from tools;
- first-interval, tool-signal and tool-result duration shares show how much of the Tokscale-style denominator those classes occupy;
- CQA exact-attribution coverage, timing coverage, quality class and qualification reasons are preserved alongside each comparison row;
- tool-excluded output includes task coverage, tool-pairing coverage, tool/wait seconds, direct reasoning/visible seconds, residual time, TTFT P50/P90, and visible-attribution coverage.

A tool-signal/result flag means tool telemetry occurred between timing boundaries. It is **not** a direct measurement of tool runtime. These variants are research diagnostics, not production performance metrics.

Rows are emitted for the whole selected workflow, model, model+effort, and report-local session. Session identifiers are remapped to `session-001`, `session-002`, etc.

## Privacy contract

The comparison files are designed to be shareable for analysis. They contain no:

- prompts or user-message text;
- model response text;
- tool output/content;
- file contents;
- source paths;
- raw Codex session/thread IDs;
- authentication or account identity data.

The implementation deliberately inspects raw records only to classify timing/accounting boundaries and immediately reduces them to numeric/content-free telemetry. Trusted spawn activation can be used internally to suppress inherited pre-activation fork history, but raw linkage identifiers are never exported.

## Validation status

CQA does **not** automatically execute an installed third-party `tokscale` binary. The output records whether such a binary was detected, but the headline remains labelled `Tokscale-style` either way.

The release test suite contains regression fixtures for non-overlapping durations, duplicate/zero snapshot handling, user-message cursor reset, tool-result interval classification, divergence from exact `AgentMessage` timing, exact tool-span subtraction, incomplete tool pairing, invalid reasoning spans, and privacy canaries. The non-overlapping timing fixture is based on the behavior introduced by upstream Tokscale PR #878.

The tool-excluded semantics were checked against a five-session raw multi-agent workflow containing nine tasks, 84 model tool calls with 84 matching outputs, timed Reasoning/AgentMessage spans, waits/subagent activity, and one malformed reasoning duration. The parser paired all 84 calls without retaining call IDs/content and rejected the malformed duration from the direct reasoning diagnostic without affecting exact tool-span subtraction.

An exact-population upstream Tokscale run over a separate 184-session workflow matched the Tokscale-style reconstruction's message/output population exactly and matched aggregate timing essentially exactly (Astra and auto-review exactly at millisecond export precision; Sol differed only by the reconstruction's single untimed entry). This validates the reconstruction for that observed population, not every future Tokscale parser revision.

Tokscale has additional fork/replay/deduplication logic that is not copied wholesale into CQA. CQA applies its own trusted child-activation filter to reduce inherited history for same-workflow comparisons. Therefore close agreement is informative, but exact equality with every possible upstream Tokscale run is not promised.

## Interpreting results

The harness now separates three complementary questions:

1. **Visible generation tok/s:** once exactly attributable visible text is being emitted, how fast is it generated?
2. **Tokscale-style output throughput:** how much non-reasoning output appears over broad accounting intervals that can include tools/waits?
3. **Tool-excluded output rate:** how much non-reasoning model output is produced per observed response time when reasoning/latency remain counted but exactly paired external tool spans are stopped out? This is the production dashboard's primary response-efficiency rate in 0.7.0.

Do not use research or dashboard rows as controlled model rankings: the workloads may differ by model/effort. Inspect evidence coverage and the time decomposition before interpreting differences.
