# Usage guide

Use the [README](../README.md) for an overview and the recommended Codex plugin setup. This guide covers the standalone CLI and optional analysis features.

- [Installation](#installation)
- [Generate and revisit reports](#generate-and-revisit-reports)
- [Select and interpret workflows](#select-and-interpret-workflows)
- [Roles and assignments](#roles-and-assignments)
- [Review pauses](#review-pauses)
- [Quota analysis and banked resets](#quota-analysis-and-banked-resets)
- [Charts and exports](#charts-and-exports)
- [Price overrides and diagnostics](#price-overrides-and-diagnostics)

## Installation

Python **3.10 or newer** is required. CQA reads existing Codex JSONL logs; it does not need an API key. The core package has no third-party runtime dependencies. The Codex plugin bundles that package and does not require `pip install`; follow the [plugin instructions in the README](../README.md#recommended-install-the-codex-plugin), then start a new Codex thread.

For standalone use, clone the repository and install the package. PyPI publication is deferred.

### Linux / macOS

```bash
git clone https://github.com/dayowe/codex-quota-audit.git
cd codex-quota-audit
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
cqa dashboard
```

### Windows PowerShell

```powershell
git clone https://github.com/dayowe/codex-quota-audit.git
cd codex-quota-audit
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
cqa dashboard
```

In a later terminal, activate the same environment before running `cqa`. If shell activation is unavailable, invoke `.venv/bin/cqa` on Linux/macOS or `.venv\Scripts\cqa.exe` on Windows directly.

Run `cqa --version` to see the package version and the independent quota/workflow analyzer versions. For development and release checks, use the [release guide](development/releasing.md).

## Generate and revisit reports

```bash
cqa dashboard
cqa dashboard --workflow latest
cqa workflow profile latest --multi-agent-only
```

These create quota-only, combined quota/workflow, and workflow-only dashboards respectively. Reports open in the default browser. HTML is self-contained and works offline.

| Option | Effect |
| --- | --- |
| `--home /path/to/.codex` | Read another Codex data directory. |
| `--no-open` | Write the report without opening a browser. |
| `--quiet` | Suppress progress; keep final report-path output. |
| `--show-analysis-output` | Show the underlying analyzers' detailed output. |
| `--name "Frontend migration"` | Add a recognition label at creation time. |
| `--report-json` | Keep normalized `cqa-report-v1` JSON beside the HTML. |
| `--report-json PATH` | Also copy the normalized JSON to a chosen path. |
| `--output PATH` | Write HTML to a chosen path, bypassing the report library. |

Normal reports are archived under `<Codex home>/codex-quota-audit/reports/`. The library maintains `index.html`, a local `catalog.json`, and stable copies under `latest/`: `quota.html`, `workflow.html`, and `combined.html`. JSON companions are opt-in. Default Codex home is `~/.codex`.

```bash
cqa reports
cqa reports list
cqa reports list --type workflow
cqa reports open latest-workflow
cqa reports open "Frontend migration"
```

`cqa reports` opens the library in a browser. `open` also accepts a report ID, a unique friendly name, `latest`, `latest-quota`, or `latest-combined`. Reopening saved reports does not rescan logs. Friendly labels are user-provided metadata; avoid including private details in labels you intend to share.

For machine-readable validation, install the optional schema validator in the same environment:

```bash
python3 -m pip install '.[validate]'
cqa dashboard --report-json --no-open
cqa report validate ~/.codex/codex-quota-audit/latest/quota.json
```

On Windows, use `python` and the corresponding local report path. The [report contract](cqa-report-v1.md) defines the normalized format.

## Select and interpret workflows

Discover local candidates, then profile a selector from the output:

```bash
cqa workflow candidates --top 15 --recent-days 45
cqa workflow profile W-YOUR_FAMILY
cqa workflow profile S-YOUR_SESSION
cqa workflow profile YOUR_CODEX_SESSION_ID
```

Families are reconstructed from recorded parent/session linkage, not just nearby timestamps. Discovery includes standalone sessions and ranks candidates by linkage and usage evidence. No linked descendants in the available logs does not prove that no workers ran.

`W-...` selects a discovered family. `S-...` selects a hashed session key; an exact local Codex session/thread ID is also accepted. A member session resolves its containing family. A family key can change as logs are added, so a session selector is useful for revisiting a growing workflow.

CLI `latest` looks back 90 days by default (`--recent-days` changes this). It prefers linked workflows with usage and can fall back to a solo session. To require delegated non-Guardian worker usage and exclude the active Codex session when its ID is available:

```bash
cqa workflow profile latest --multi-agent-only
cqa dashboard --workflow latest --workflow-multi-agent-only
```

The plugin uses this stricter selection for requests to profile the latest multi-agent workflow. If no qualifying workflow exists, it reports that limitation.

For a particular subtree or time window:

```bash
cqa workflow profile YOUR_SESSION_ID --analysis-root YOUR_SESSION_ID
cqa workflow profile YOUR_SESSION_ID \
  --after 2026-09-29T00:00:00+02:00 \
  --before 2026-10-04T00:00:00+02:00
```

`--after` / `--before` require timezone-aware timestamps; `--before` is exclusive. Windowed runs handle carry-in workers conservatively. See [window handling](workflow-profiler.md#analysis-windows-and-restarts) before using a cutoff for before/after comparisons.

Read direct agent usage first, then inspect roles, subtrees, context, and timing. Direct totals are additive; inclusive subtree totals overlap. Missing roles do not remove usage, and missing lifetime evidence does not turn into an invented interval. The [workflow methodology](workflow-profiler.md) explains these distinctions.

### Detailed exports and structural diagnostics

`--report-json` writes the normalized dashboard contract. `--export-json` writes the richer profiler format used by offline pause review and attribution diagnostics:

```bash
cqa workflow profile YOUR_SESSION_ID --export-json workflow-profile.json
```

Attach one or more existing detailed profiles to a quota dashboard:

```bash
cqa dashboard --workflow-profile-json workflow-profile.json
```

`--workflow-profile-json` is repeatable. Combined dashboards provide a workflow selector.

For lower-level lifecycle evidence or a privacy-safe discovery manifest:

```bash
cqa workflow candidates --export-json workflow-candidates.json
python3 -m cqa.workflow.lifecycle --family W-YOUR_FAMILY --export-json workflow-lifecycle.json
```

Lifecycle extraction separates trusted, diagnostic, and unresolved matches. Diagnostic timing hints do not create cost attribution. The module's `--help` lists its controls.

## Roles and assignments

Normal profiling uses a generic interpretation: solo sessions, flat teams, nested teams, and unnamed workers all contribute. Responsibilities require explicit role evidence; root position does not imply coordinator or planner.

If you know the root's role, declare it:

```bash
cqa workflow profile YOUR_SESSION_ID --root-role coordinator
```

For several declarations, save `roles.json`:

```json
{
  "schema": "workflow-role-map-v1",
  "root_role": "manager",
  "sessions": [
    {"session": "LOCAL_SESSION_SELECTOR", "role": "researcher"}
  ]
}
```

```bash
cqa workflow profile YOUR_SESSION_ID --role-map roles.json
```

Selectors are local audit inputs; portable reports retain only resolved roles and report-local agent IDs. Conflicting explicit declarations fail rather than silently replacing evidence. Combined dashboards use `--workflow-root-role` / `--workflow-role-map` for the same declarations.

`--roles researcher writer editor` changes the vocabulary recognized in recorded metadata and labels. It does not assign those roles or classify prompt prose. Include all names you want recognized. Optional structured assignment labels and assignment maps can add task-level attribution; see [assignment identity](workflow-profiler.md#assignment-identity) for the format and evidence requirements.

For a known implementation/validation sequence, opt into `--workflow-profile staged`. Custom sequences can use `--cycle-roles FIRST SECOND` and repeatable `--successor FIRST=SECOND` with configured roles. Generic mode assumes no sequence. `--stage-roles` requests a separate filtered activity view and does not filter primary totals or concurrency.

## Review pauses

Profiling flags gaps of at least one hour without recorded calls across the analyzed workflow (`--quiet-gap-minutes` changes the threshold). Silence is a candidate, not an automatic pause classification. It can also reflect tests, waiting, or missing telemetry.

Save a detailed profile, then review it interactively:

```bash
cqa workflow profile YOUR_SESSION_ID --export-json workflow-profile.json
python3 -m cqa.workflow.profile --review-pauses workflow-profile.json
```

Review reads the saved snapshot without rescanning logs. You can confirm boundaries, edit timezone-aware timestamps, reject a candidate as ordinary elapsed time, clear a decision, or add another pause. Enter keeps a decision; finish review to save. Ctrl-C/EOF cancels unsaved changes.

Decisions are stored under `$XDG_STATE_HOME/codex-quota-audit/pauses/`, defaulting to `~/.local/state/codex-quota-audit/pauses/`. They follow the stable selected session root, so future profiles can reuse them as a family grows. Use `--pause-store DIRECTORY` on both profiling and review to choose another location.

The source profile remains unchanged. To save a revised copy using the snapshot and current decisions:

```bash
python3 -m cqa.workflow.profile --review-pauses workflow-profile.json \
  --export-json workflow-profile-reviewed.json
```

The revised file must be a new path. Older detailed exports without a compact usage timeline need regenerating before arbitrary boundary edits. See [pause accounting](workflow-profiler.md#quiet-intervals-and-pause-accounting) for adjusted-rate semantics.

## Quota analysis and banked resets

The quota analyzer reads available logs under `~/.codex/sessions/` and `~/.codex/archived_sessions/`, without a built-in history cutoff. It targets the 7-day quota window by default; other recorded windows remain available for diagnostics.

```bash
cqa audit
cqa audit --history
cqa audit --diagnostics
```

`--history` adds monthly/model trends and detected regime tables. `--diagnostics` (alias `--verbose`) adds reset, replay, telemetry, and Guardian diagnostics. Guardian analysis is included when data exists; `--no-guardian-audit` skips it.

For banked-reset analysis, pass timestamps of resets you personally confirmed:

```bash
cqa dashboard \
  --banked-reset 2026-09-05T23:11 \
  --banked-reset 2026-09-10T08:23
```

Hour-, minute-, and second-resolution timestamps are accepted; a timestamp without an offset uses the machine's local timezone. `--banked-reset` is repeatable. The audit compares matched whole periods and equal quota slices around each reset. Default slices are 5, 10, 14, and 20 points; repeat `--banked-slice-points POINTS` to choose others.

See the [quota methodology](quota-methodology.md) for matching, uncertainty, and interpretation. An early reset alone does not establish a banked reset.

## Charts and exports

Publication PNG/SVG charts require `matplotlib` in the environment where CQA is installed:

```bash
python3 -m pip install matplotlib
cqa audit --charts
cqa audit --charts --chart-theme dark
```

Light is the default chart theme. Charts write to the current working directory using configurable filename prefixes. Common outputs are `quota_value_by_model_effort.png` / `.svg` and `quota_chart_data.csv`, with `guardian_approval_overhead`, `guardian_quota_by_period`, `banked_reset_capacity`, and `banked_reset_boundary_slices` image pairs when those analyses have enough evidence.

For a Markdown report and legacy summary JSON:

```bash
cqa audit --charts --report quota-report.md --summary-json quota-summary.json
```

The legacy summary format is distinct from `cqa-report-v1`. CSV exports can be requested independently:

| Option | Data |
| --- | --- |
| `--export-buckets PATH` | High-water quota buckets. |
| `--export-resets PATH` | Reconstructed reset ledger. |
| `--export-chart-data PATH` | Model/effort chart aggregates. |
| `--export-approval-episodes PATH` | Guardian approval episodes. |
| `--export-guardian-periods PATH` | Guardian quota estimates by reset period. |
| `--export-banked-capacity PATH` | Whole-period banked-reset comparisons. |
| `--export-banked-slices PATH` | Equal-quota before/after slices. |

For example:

```bash
cqa audit --banked-reset 2026-09-10T08:23 --export-banked-slices banked-slices.csv
```

Exports can describe detailed activity patterns. Review them before sharing; the local catalog and raw research staging are outside the portable report privacy contract. See [privacy](privacy.md).

For compatibility, `cqa audit --dashboard` writes the fixed `~/.codex/codex-quota-audit/report.html` path, or accepts an explicit HTML path. Prefer `cqa dashboard` for archived reports and automatic browser opening.

## Price overrides and diagnostics

Override or extend the bundled normalization table with a local JSON file:

```json
{
  "gpt-example": {"input": 4.0, "cached": 0.4, "output": 20.0}
}
```

Values are dollars per million uncached-input, cached-input, and output tokens. A three-element array `[4.0, 0.4, 20.0]` is also accepted. Pass `--prices prices.json` to `cqa dashboard`, `cqa audit`, or `cqa workflow profile`. A `codex-auto-review` entry overrides the built-in date-aware mapping. These values normalize work; they do not represent subscription billing.

`cqa audit --weight-details` exposes experimental token-type quota-weight fits. `--diagnostics` compares replay-filtered results with probable replay prefixes included; `--include-replays` explicitly includes those prefixes in the analysis. See [quota methodology](quota-methodology.md#diagnostic-and-weight-analysis) before interpreting either.

For throughput-semantics research, use `cqa research throughput-compare ID` and the [research guide](research/throughput.md). Raw-log Tokscale staging is local-only and is not a shareable report.

Use `cqa --help`, `cqa dashboard --help`, `cqa workflow profile --help`, or `cqa audit --help` for the full option lists.
