# Architecture

Codex Quota Audit has one editable implementation: `src/cqa/`.

```text
local Codex telemetry
        |
        +-- cqa.quota.audit -------- quota / Guardian / banked-reset analysis
        |      +-- usage.loader --- shared extraction/cache/dedup/replay loading
        |
        +-- cqa.usage.analysis ----- calendar totals / coverage / pricing
        |      +-- usage.cli ------- terminal / HTML / CSV consumers
        |
        +-- cqa.workflow.profile --- workflow attribution / pricing / timing
        |      +-- candidates
        |      +-- lifecycle
        |      +-- attribution
        |      +-- pauses
        |      +-- response_efficiency
        |
        v
   cqa-report-v1                 privacy-safe presentation contract
        |
        +-- JSON
        +-- self-contained HTML dashboard
        +-- plugin / future clients
```

`cqa.cli` is a thin orchestrator. Analytical logic belongs in the quota/usage/workflow
packages, not in the browser, plugin skills, or CLI glue.

Quota and calendar usage share a loader and the existing metadata/token consumer. The quota compatibility entry point retains its meter-backed extraction semantics; the usage projection also keeps valid observations without meters. Cache extraction versions are independent by kind, preserving prior discovery/quota/telemetry entries. Dates, prices and model filters are applied after extraction; replay classification precedes calendar selection. Daily aggregates use the requested calendar timezone. The shared HTML renderer displays a usage view and the local library archives it as report type `usage`, without a second extraction pass. Calendar aggregates live in the additive `report.extensions.usage` payload and do not change the frozen report schema or run workflow/quota inference.

## Repository layout

```text
src/cqa/                              canonical Python package
plugins/codex-quota-audit/            Codex plugin metadata + skills
plugins/.../runtime/cqa/              generated self-contained runtime mirror
tests/unit/                            contract/unit tests
tests/integration/                     CLI/plugin/release integration tests
tests/regression/                      bug/semantics regression tests
tests/fixtures/                        small checked-in fixtures
tools/                                 release/development tooling
docs/                                  user/developer/research documentation
schema/                                public frozen report-schema copy
compat/                                deprecated direct-script wrappers
```

The plugin runtime is generated from `src/cqa` by `tools/sync_plugin_runtime.py`.
It is tracked only so marketplace installs remain self-contained; it must never
be edited directly. `tools/release_check.py` fails if the mirror drifts.

The dashboard template and packaged schema live canonically under
`src/cqa/assets/`. The public schema copy under `schema/` is generated and
release-checked. There is no independently editable top-level dashboard copy.

## Report contract

`cqa-report-v1.0` is frozen. Breaking structural or semantic changes require a
new major report schema version. Unavailable values remain `null`, report-local
IDs replace raw Codex identifiers, and browser JavaScript renders/filters only;
it does not re-run analytics.

## Privacy

Normal dashboard reports use `dashboard-safe-v1` and exclude prompt/response
text, tool output, source file contents/paths, auth/account identity, and raw
session identifiers. See `privacy.md`.
