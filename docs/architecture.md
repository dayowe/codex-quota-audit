# Architecture

Codex Quota Audit has one editable implementation: `src/cqa/`.

```text
local Codex telemetry
        |
        +-- cqa.quota.audit -------- quota / Guardian / banked-reset analysis
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

`cqa.cli` is a thin orchestrator. Analytical logic belongs in the quota/workflow
packages, not in the browser, plugin skills, or CLI glue.

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
