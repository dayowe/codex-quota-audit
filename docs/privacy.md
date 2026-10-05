# Privacy model

Codex Quota Audit processes local Codex telemetry and is designed so the standard report can be inspected or rendered without exposing conversation contents.

## Dashboard-safe contract

`cqa-report-v1` uses privacy profile `dashboard-safe-v1`. Standard reports exclude:

- prompt and response text;
- tool stdout/output;
- source-code and file contents;
- authentication/account identity;
- source filesystem paths;
- raw Codex session/thread identifiers.

Workflow relationships are represented with report-local IDs.

The HTML dashboard is self-contained: no CDN, remote fonts, analytics, external scripts, or telemetry endpoints.

## Privacy canary regression test

The release suite deliberately injects unique fake secrets into source-side fields, including a prompt canary, tool-output canary, private filesystem path, and UUID-like session identifier. It then builds a combined report containing quota, Guardian, banked-reset, and workflow sections and renders both JSON and HTML.

The test fails if any canary appears in either generated artifact. This supplements structural forbidden-key/path checks and helps catch accidental propagation through new fields.

Run the complete local gate with:

```bash
python3 tools/release_check.py
```

This test is a regression guard, not a formal proof that arbitrary future code can never leak sensitive data. New report fields must still be reviewed against the privacy contract.

## Workflow throughput privacy

Turn-throughput aggregation uses only deduplicated usage timestamps, model labels, role labels, and token counts already present in the privacy-safe workflow analysis. Model/role aggregates do not require prompt text, responses, tool output, source paths, or raw session IDs. **Model output / elapsed min** uses response-level output-token totals and the workflow analysis-window duration only. Agent throughput enters `cqa-report-v1` only after agent identities are remapped to report-local IDs.

Optional role annotations are local audit inputs. `--root-role` contains only a role label. `workflow-role-map-v1` may use raw local session selectors to identify sessions, but those selectors are resolved before report construction and are never exported; only the resolved role/evidence attached to privacy-safe report-local agent IDs can enter `cqa-report-v1`.

## Local report catalog

The unified `cqa` front end keeps a local report library under `<Codex home>/codex-quota-audit/`. `catalog.json` is local application state, not part of the portable `dashboard-safe-v1` report contract. It may retain the local workflow selector used to produce a report so a user can correlate an artifact back to its source on the same machine.

Shareable HTML/report JSON and report filenames continue to use only privacy-safe report-local IDs / short `wf-...` references. Raw Codex session/thread IDs are not placed in portable report contents or filenames. Friendly labels, when supplied with `--name`, are user-provided metadata; CQA does not inspect prompts/source code to invent semantic labels automatically.

## Timing-performance privacy

Timing-qualified performance uses only turn timestamps/durations, token counts, model/effort labels and already-derived workflow role/agent attribution. Raw `turn_id` values are used only transiently to join events inside one local rollout; the parser converts them to one-way local hashes before intermediate timing objects are finalized, and `cqa-report-v1` does not export the raw or hashed turn identifier.

Visible-output tok/s qualification does not require exporting visible response text. It uses only the visible `AgentMessage` duration and aggregate token counts. Privacy-canary coverage exercises the same raw-telemetry path and fails if source prompt/tool canaries or raw session identifiers reach generated JSON/HTML.
