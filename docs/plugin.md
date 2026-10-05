# Codex plugin packaging

Codex Quota Audit ships as a self-contained repository marketplace plugin in
addition to the standalone Python package.

## Install

Requires Python 3.10 or newer on the launcher path (`python3` on Linux/macOS, `python` on Windows), local Codex logs, and a Codex CLI with plugin support. See the [README](../README.md#get-started) for user setup and the [usage guide](usage.md) for standalone commands.

```bash
codex plugin marketplace add dayowe/codex-quota-audit --ref main
codex plugin add codex-quota-audit@dayowe
```

Start a new Codex thread after installation so the skills are discovered.
For local development, add the repository checkout as the marketplace source.

The marketplace catalog is `.agents/plugins/marketplace.json`; it points to
`plugins/codex-quota-audit/`.

## Skills

The plugin exposes exactly two user-facing skills:

- `quota-audit` — quota efficiency, model/effort cohorts, policy regimes,
  Guardian, banked resets, and combined dashboards.
- `workflow-profile` — workflow discovery, attribution, performance, pricing,
  compactions, concurrency, pauses, Guardian activity, and timelines.

The skills are thin launchers. They add the bundled plugin runtime to
`sys.path` and invoke `cqa.cli`; analytical logic never lives in the skill
files.

## Self-contained runtime without two editable implementations

Canonical source is:

```text
src/cqa/
```

The plugin needs to work without a separate `pip install`, so releases carry a
generated mirror:

```text
plugins/codex-quota-audit/runtime/cqa/
```

Refresh/check it with:

```bash
python3 tools/sync_plugin_runtime.py
python3 tools/sync_plugin_runtime.py --check
```

Do not edit the runtime mirror directly. The release gate verifies byte-for-byte
synchronization with canonical source.

## Latest multi-agent workflow behavior

The plugin phrase “Profile my latest multi-agent workflow” uses stricter
selection than the broad standalone `latest` selector: it requires delegated
non-Guardian worker usage and excludes the active Codex session when Codex
exposes its ID. It does not silently fall back to Guardian-only or standalone
activity.

Reports are archived under `~/.codex/codex-quota-audit/`. Normal skill
invocations use `--quiet`, so successful runs print only final local paths.

## Privacy/local behavior

The plugin has no remote service, MCP backend, analytics endpoint, CDN, or
telemetry service. Standard reports use `dashboard-safe-v1`; see `privacy.md`.

## Validation

```bash
python3 tools/release_check.py
```

Focused development commands:

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m cqa.quota.audit --self-test
PYTHONPATH=src python3 -m cqa.workflow.profile --self-test
PYTHONPATH=src python3 -m cqa.workflow.candidates --self-test
python3 tools/sync_plugin_runtime.py --check
```

A literal marketplace install/new-thread smoke still requires a machine with the
real `codex` executable and is a manual release-candidate check.
