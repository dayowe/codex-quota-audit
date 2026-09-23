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
- Where inside a multi-agent workflow are orchestration, implementation, validation, context rereads, and re-entry consuming the most work?

The script reads local Codex JSONL telemetry under `~/.codex`, reconstructs effective quota accounting periods, filters replayed history, and relates observed token usage to the Codex rate-limit meter.

**Nothing leaves your machine.**

> [!IMPORTANT]
> This is an empirical analysis of local telemetry. It is not documentation of OpenAI's internal quota formula, billing system, or compute costs.

---

## Quick start

For quota analysis:

```bash
python3 codex_quota_audit.py --charts
```

This prints the highest-value findings and creates publication-ready PNG/SVG charts.

### Profile a session or agent workflow

For workflow costs, start with any session ID from the run you want to inspect:

```bash
python3 profile_workflow_cost.py \
  --session YOUR_SESSION_ID \
  --export-json workflow_cost_profile.json
```

This works with solo sessions, unnamed workers, flat teams and nested agent trees.
No staged-implementation skills, special agent names or project configuration are
required. The default **generic** profile reports observed usage, context growth,
compactions and linked parent/worker activity without assuming a plan → implement
→ validate sequence. Missing linkage or roles are reported as limitations.

If you do not know the session ID, run `python3 find_workflow_candidates.py` and
choose a reported `W-...` family or `S-...` session key. A session selector resolves
the containing family; use `--analysis-root YOUR_SESSION_ID` as well to select only
that session's observed subtree.

Workflow analysis uses the Python standard library. Clone this repository, or keep
these six modules together: `profile_workflow_cost.py`, `find_workflow_candidates.py`,
`extract_workflow_lifecycle.py`, `workflow_attribution.py`, `workflow_pauses.py` and `codex_quota_audit.py`.
They read local logs under `~/.codex`; `--home /path/to/codex-home` selects another
log location. They make no network requests. The JSON export is optional.

Long breaks can lower tokens/hour without improving the workflow. The profiler
flags quiet intervals; [pause review](#quiet-intervals-and-confirmed-pauses) lets you
confirm them once and reuse the decisions without rescanning logs during review.

**API$eq is a comparison measure, not your subscription bill or quota consumption.**
Raw tokens, cached input and price coverage remain visible. Reports cannot establish
that observed overlap, context growth or validation effort was unnecessary.

For a known implementation/validation workflow, add `--workflow-profile staged`.
For other role names, see [Custom workflows](#custom-workflows). Neither option
changes which roles count toward core token totals or concurrency.

For practical setup and interpretation, see
[Getting useful results and identifiable roles](#getting-useful-results-and-identifiable-roles).

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

## Experimental workflow candidate finder

The repository also includes `find_workflow_candidates.py`, a privacy-conscious helper for locating representative multi-agent workflows before building or tuning a workflow cost profile.

It reconstructs parent → subagent families from session/thread/rollout linkage metadata rather than grouping sessions only by time. It also recognizes configurable role labels such as `orchestrator`, `planner`, `implementer`, and `validator`, while avoiding prompt/response text in its output.

Run:

```bash
python3 find_workflow_candidates.py
```

The default output shows the 10 best recent graph-linked workflow families, including:

- root and child session structure
- inferred child roles when strong metadata supports them
- model and reasoning effort
- approximate token scale and cache share
- spawn/send/wait/resume/close lifecycle signals
- parent-link method and confidence

For a shareable machine-readable manifest:

```bash
python3 find_workflow_candidates.py \
  --export-json workflow_candidates.json
```

The helper one-way hashes linkage identifiers and does not print prompts, responses, source code, tool stdout, or raw session/thread IDs. It is a **sample-selection tool**; use `extract_workflow_lifecycle.py` for conservative per-workflow lifecycle and cost analysis.

Useful options:

```bash
python3 find_workflow_candidates.py --top 15
python3 find_workflow_candidates.py --recent-days 45
python3 find_workflow_candidates.py --show-link-schema
python3 find_workflow_candidates.py --self-test
```

---


## Experimental workflow profiling helpers

The repository also includes three helper commands for investigating Codex sessions and agent workflows. Analysis never edits source logs; optional pause review saves local annotations. These helpers remain separate from the main quota audit while the workflow event schema is being validated against real rollout logs.

### 1. Find graph-linked workflow families

```bash
python3 find_workflow_candidates.py
```

`find_workflow_candidates.py` scans local rollout metadata and reconstructs candidate parent/subagent families from session/thread/rollout linkage IDs. It does not print prompts, model responses, source code, tool stdout, or raw linkage IDs.

For each recent family it reports:

- root and linked child sessions
- strong role labels when present
- model and reasoning effort
- approximate token scale and cache share
- structural lifecycle action counts such as spawn/send/wait
- link method and confidence

The generated `W-...` family IDs are useful for selecting a representative workflow. A root/member `S-...` session key is more stable if a family later grows as new linked logs are added.

Discovery includes standalone sessions and ranks families by observed linkage and
usage coverage, not particular job titles. “Standalone” means no linked descendants
were found in the available logs; it does not prove that no agents were used.

Optional privacy-safe JSON manifest:

```bash
python3 find_workflow_candidates.py \
  --export-json workflow_candidates.json
```

### 2. Extract one workflow's structural lifecycle and cost profile

After choosing a family, run:

```bash
python3 extract_workflow_lifecycle.py \
  --family W-e7af89b98f
```

You can also select a family using any member/root session key:

```bash
python3 extract_workflow_lifecycle.py \
  --family S-644a110a4f
```

This is useful when an older `W-...` family ID changes because additional linked rollout files were created later.

The v2 lifecycle extractor is deliberately conservative. It rescans only the selected family's rollout files and separates lifecycle evidence into three classes:

- **trusted**: exact action/result target IDs, or a unique child session beginning within the very tight spawn/start window
- **diagnostic**: plausible hints such as a single active agent or broad graph/time proximity
- **unresolved**: no sufficiently reliable target could be established

Only **trusted** actions are allowed to create follow-up phases or role-targeted supervision totals. Low-confidence `single-active-agent` guesses remain visible for debugging but are never used for cost attribution. A caller is also never allowed to resolve itself as its own action target.

The extractor also deduplicates repeated representations of the same lifecycle action and keeps `codex-auto-review` explicitly labeled as Guardian. Root position is separate from responsibility: a root may be Coordinator, Orchestrator or unknown. Exported sessions include immediate-parent evidence and identity conflicts; routing to an orchestrator does not establish the caller's role.

It reports:

- trusted / diagnostic / unresolved lifecycle coverage by action type
- structural ID availability (`call_id`, target IDs, result IDs)
- number of duplicate lifecycle representations removed
- token usage by root/orchestrator, implementer, validator, Guardian, and other roles
- initial agent work vs **trusted** follow-up rounds
- number of trusted follow-ups sent to each agent
- parent inference occurring around spawn/send/wait/close decisions
- large-context, small-output inference requests that may represent expensive supervision/context rereads
- the most expensive individual inference requests
- a privacy-safe structural timeline with attribution class shown on every action

For a shareable machine-readable extract:

```bash
python3 extract_workflow_lifecycle.py \
  --family W-e7af89b98f \
  --export-json workflow_lifecycle.json
```

The JSON export contains structural metadata and token counts only. It does not contain prompts, responses, source code, tool output, or raw agent/session/thread IDs. The v2 export also includes an `action_match_audit` and separates `target` from `diagnostic_target`.

Useful options:

```bash
python3 extract_workflow_lifecycle.py --help
```

The default trusted temporal fallback for a spawn requires a unique child session to begin within 2 seconds of the spawn call. You can tune that diagnostic boundary with:

```bash
python3 extract_workflow_lifecycle.py \
  --family W-e7af89b98f \
  --tight-spawn-seconds 2
```

Parent/action inference association is still a timing heuristic and is explicitly reported as non-causal. Lifecycle schemas can change between Codex versions, so unresolved events are retained instead of being silently assigned to whichever agent happens to be active.

### 3. Profile session and workflow costs

Once a representative family has been validated, use `profile_workflow_cost.py` to answer the workflow-optimization question directly:

```bash
python3 profile_workflow_cost.py \
  --family W-e7af89b98f \
  --export-json workflow_cost_profile.json
```

A stable member/root session key works too:

```bash
python3 profile_workflow_cost.py --family S-644a110a4f
```

Or pass any member's exact Codex session/thread ID directly; no finder step is needed:

```bash
python3 profile_workflow_cost.py \
  --session YOUR_SESSION_ID \
  --export-json workflow_cost_profile_new.json
```

`--session-id` is an alias; `--family` also accepts the raw ID. The lifecycle
extractor supports the same selectors. Matching fingerprints the supplied ID
locally and resolves its containing family, rejecting ambiguous metadata matches.
Reports retain hashed IDs. A session selector does not change the analysis root
or isolate a subtree; existing root-selection and date-window rules still apply.
The matching logs must be available under `--home` (default `~/.codex`).

The workflow cost profiler is currently **v6.1**. It deliberately does **not** require exact `SEND` / `WAIT` session-recipient recovery.

### Quiet intervals and confirmed pauses

Normal profiling automatically flags intervals of at least **one hour without
recorded model calls across the analyzed family/subtree**. It never prompts or
automatically classifies silence as a pause. A quiet coordinator with busy workers
does not qualify. Long tests, external waits and missing telemetry can also cause
silence. Only gaps bounded by recorded calls are suggested, not leading/trailing
silence at the report edges. Change the threshold with `--quiet-gap-minutes` if needed.

The output keeps the full elapsed-time rate and shows a separate hypothetical rate
excluding unclassified gaps. This is a sensitivity check, not an efficiency claim.
To confirm whether a suggested interval was a deliberate pause, use the saved report:

```bash
python3 profile_workflow_cost.py --review-pauses workflow_cost_profile.json
```

This explicitly interactive command **does not rescan Codex logs or launch agents**.
It requires a terminal; normal profiling remains suitable for unattended scripts.
For each interval, choose:

1. Confirm the displayed boundaries (approximate if inferred from calls).
2. Confirm with edited, timezone-aware timestamps.
3. Mark as ordinary elapsed time; do not suggest excluding it again.
4. Remove the decision / leave unclassified.

Enter keeps the current decision. At the end you can add a pause that was not
suggested, such as a shorter break or a pause during which workers kept running.
Edited timestamps must lie inside the saved report's window. Displayed times use
your local timezone and show its UTC offset. Finish review to save; Ctrl-C/EOF
cancels unsaved changes. Existing decisions can be edited or removed with the same
command. Decisions outside the selected report window are retained.

Decisions are stored locally under
`$XDG_STATE_HOME/codex-quota-audit/pauses/`, defaulting to
`~/.local/state/codex-quota-audit/pauses/`. Files contain a hashed analysis-root key,
timestamps, classification and boundary provenance, with private file permissions.
They are outside the repository, contain no conversation text, and are not uploaded.
Use `--pause-store DIRECTORY` on both profiling and review to choose another store.
Normal profiling only reads this store; missing stores are not created.

Annotations follow the **stable selected session root**, even as its family grows;
they do not follow temporary `G1` labels or a changing `W-...` family key. Profiling
a different explicit subtree root uses separate decisions. Changed gap evidence
can produce a new unclassified candidate; saved confirmed intervals still apply,
and any newly observed usage within them is reported.

Future profiles automatically show both measurements, for example:

```text
Elapsed: 46.20h; confirmed pauses: 8.59h; excluding pauses: 37.61h
Raw tokens/elapsed hour:          17.27M
Raw tokens/hour excluding pauses: 21.21M
```

All original token/request totals remain intact. If calls were recorded during a
confirmed pause, their usage is shown separately and removed **along with the time**
from the adjusted rate. Intervals are start-inclusive/end-exclusive; overlapping
pauses count once and are clipped to the analysis window. Inferred gaps start just
after the preceding call and end at the following call, preserving both bounding
requests. If pauses cover the entire window, the adjusted rate is unavailable.

“Excluding pauses” is **not active CPU time or productive time**. Calls are assigned
by recorded timestamp, not execution duration; a long-running response may cross a
pause boundary. Other report fields, including concurrency, compaction and existing
comparison metrics, keep their original semantics and are not silently adjusted.
Compare similarly defined time periods and keep workload differences visible.

Review uses a v6.1+ export's compact timestamp/request/token timeline. Earlier exports
lack the precision required for arbitrary boundary edits: regenerate them once with
v6.1+ rather than guessing from burst totals. The source report stays unchanged.
To also save a revised copy after review, choose a new output path:

```bash
python3 profile_workflow_cost.py --review-pauses workflow_cost_profile.json \
  --export-json workflow_cost_profile_reviewed.json
```

The copy refreshes `pause_analysis` only, using the saved snapshot and current local
decisions. Existing output files are not overwritten. Conflicting concurrent reviews
fail explicitly rather than silently replacing another review's decisions.

### Getting useful results and identifiable roles

**Start with the logs you already have.** Run the session-ID command above, even
if the main agent chose its own workers and you never defined any roles. You can
still compare the root's own usage, individual workers and linked subtrees, plus
context growth and compactions where recorded. Missing role evidence produces
`unknown`, not missing tokens. Missing parent/spawn evidence limits tree and
lifetime analysis; the report exposes that separately.

Read **direct session usage** first to locate expensive agents, then inspect their
subtrees and context/compaction measures. Direct totals are additive; inclusive
subtree totals overlap and must not be summed together. These observations locate
work, but do not establish what it accomplished or whether it was unnecessary.

#### Role recognition is optional, and requires evidence

The default vocabulary (`coordinator`, `orchestrator`, `planner`, `implementer`,
`validator`) comes from the workflows this tool initially analyzed. It is not a
required team structure or a universal Codex role standard. The parser recognizes:

| Evidence in the available logs or mapping | What it establishes |
| --- | --- |
| An exact recognized role in supported fields of the session's own `session_meta` | Declared role; supported field names include `role`, `agent_role`, `agent_type`, `role_name` |
| A recognized role in the final component of the worker's own recorded `agent_path` | Weaker naming evidence for a role, not a chunk/assignment identity |
| A supported structured task label in a spawn matched to that worker | Declared role and assignment identity |
| A supplied, validated assignment map | Explicitly declared role and, optionally, parent/assignment identity |

For example, `researcher_1` can provide weaker role evidence **if** the runtime
records it as the worker's own `agent_path` leaf and `researcher` is in `--roles`.
That name alone does not establish an assignment. Metadata availability varies;
do not assume that every runtime records these fields.

Writing “you are a researcher” in a worker's prompt or final response is not enough:
the profiler does not classify jobs from conversation prose. Similarly,
`--roles researcher writer editor` tells the parser which existing names to
recognize; it does not create log metadata or assign roles to sessions. Guardian
classification is handled separately from recorded auto-review model metadata.

#### Optional preparation for future runs

Simple role metadata/names are sufficient for a role breakdown when recorded.
If you also want reliable **per-task assignment attribution**, use the optional
[structured label helper](#tool-compatible-assignment-identity). No staged skills
or implementation/review sequence are required. Give the parent agent this guidance:

> When spawning workers, use the audit tool's `workflow_attribution.encode_label`
> helper to generate task labels with a consistent role, a run ID and a task ID.
> Put the generated string in the spawn tool's `task_name` argument where supported,
> not merely in the task prompt. Follow the documented attempt/reuse convention.
> Do not ask workers to write telemetry or edit Codex logs. If the tool cannot carry
> the label, preserve an existing explicit worker mapping instead of inventing one.

For a research task, generate a label from this repository directory:

```bash
python3 -c 'from workflow_attribution import encode_label; print(encode_label("research-01", "researcher", 1, run_id="run-a"))'
```

The **parent** supplies that output as `task_name` when launching the worker. Label
generation alone does not put anything into the logs. A trusted match between the
recorded spawn and child session is also required. The main/root session has no
parent spawn label; leave its role unknown unless its own metadata or an explicit
mapping establishes it. Its position as root remains identifiable regardless.

Analyze with the vocabulary used by the workflow:

```bash
python3 profile_workflow_cost.py \
  --session YOUR_SESSION_ID \
  --roles researcher writer editor \
  --export-json research_workflow_profile.json
```

`--roles` replaces the default list, so include every role you want recognized.
This command stays in generic mode; role recognition does not require stage/cycle
interpretation.

#### Existing unlabeled runs and verification

For an existing run, use an [assignment map](#optional-mapping-import) only when
you have trustworthy identity information, such as an explicit saved handoff.
Role-only entries are supported; task IDs are not mandatory. Pass the map with
`--assignment-map mapping.json` and include its custom role names in `--roles`.
Otherwise, keep `unknown` rather than guessing from timing or token volume.

Before drawing role/task conclusions, inspect `nested_attribution.sessions` in the
JSON: `role`, `role_confidence`, `parent_source`, `assignment_source`,
`role_diagnostics` and `issues` explain the evidence and conflicts. Check
`nested_attribution.coverage` and the unattributed remainder, plus
`workflow_analysis.lifetime_windows_status` and `sessions_without_lifetime_windows`.
An unknown role and a missing parent link are different limitations. Test optional
labeling on a small run before relying on it for a long workflow; do not reorganize
your workflow merely to populate every report field.

### Generic workflows: v6

Core accounting and lifetime windows are independent of role names. An unknown-role
worker with trusted spawn/lifetime evidence contributes just like a named worker.
When usage is present but a trusted spawn is missing, usage remains counted; a
lifetime window is not invented. `workflow_analysis` reports window coverage and
the sessions missing that evidence. Zero observed concurrency is not proof that no
other worker ran.

Generic mode assumes no stage order. Role-pair cycles require `--cycle-roles FIRST
SECOND` or the opt-in `staged` profile. An unavailable cycle summary is `null`, with
a reason in `workflow_analysis`, rather than a misleading zero-cost measurement.
Custom role filters produce a separate view; they never hide unknown-role work
from the core report. Structured assignment labels and optional assignment maps
still enrich per-unit attribution, but are not required for session/subtree totals.

The output schema is **`codex-workflow-cost-profile-v6.1`**. Compared with v5.1:

- `pause_analysis` (v6.1) adds quiet-interval candidates, confirmed/rejected decisions,
  a compact usage timeline for offline review, and separate elapsed/pause-adjusted
  rates. It does not change the primary accounting or existing comparison fields.
- `workflow_analysis` records the selected interpretation, lifetime coverage, cycle
  availability and optional role-filtered activity.
- Cycle fields and supervision summaries use `first`/`second` roles instead of
  hard-coded implementer/validator names. Partial, overlapping or non-isolated
  cycles do not qualify for isolated supervision ratios.
- Existing `recognized_child_*`, `peak_concurrent_children` and
  `no-recognized-child-active` keys are retained for compatibility. They now refer
  to **all descendants with trusted lifetime evidence**, regardless of role, not
  only immediate children. The nested attribution view reports immediate children.
- With a date cutoff, root selection uses structural ancestry and in-window
  activity, not Coordinator/Orchestrator role preference. Ambiguous structural
  roots require `--analysis-root S-...` (or an exact raw session ID). An explicit
  leaf root does not acquire sibling activity as a fallback.

For before/after comparisons, fix the root and time window and verify the same
session membership. Role-independent windows can change concurrency compared with
older reports without changing token accounting; that is not workflow savings.
Keep historical exports and write reruns to new filenames.

### Nested attribution (introduced in v5)

v5 adds `workflow_attribution.py` for explicit assignment identity and nested
Coordinator → Orchestrator → Implementer/Validator accounting. It also supports
historical direct-orchestrator families. Run the usual profiler command with a
**new output filename**; historical reports are not upgraded or overwritten automatically.

```bash
python3 profile_workflow_cost.py --family W-... --export-json workflow_cost_profile_v5.json
```

The new `nested_attribution` section reports:

- Each session's **direct** usage, responsibility, immediate parent and identity evidence.
- **Inclusive subtree** usage, including that session and all resolved descendants.
  These totals overlap: never sum parent and child subtree totals.
- Additive unit buckets, separated by explicit run and chunk/group identity,
  with a by-role breakdown and an unattributed remainder. Named-group lead work
  stays with the group; it is not distributed across its chunks.
- Each parent's own activity by the number of observed active **immediate** children.
  This is time-window association, not proof of supervision cost or waste.
- Assignment/parent conflicts, unresolved coverage, and optional activity after
  an explicitly declared completion. An idle/open session is not a completion event.

Direct session totals reconcile to the selected primary total. Unit buckets plus
unattributed usage also reconcile. Root-wide work remains unattributed to chunks
unless supported by explicit assignment evidence; it is never allocated by time
alone. Cached input remains a subset of input; reasoning remains a subset of output.
Missing model prices do not erase tokens and are exposed through price coverage.

Old root-only `orchestrator_*` comparison keys are now `root_*`; `analysis_root`
replaces `analysis_orchestrator`. Root role is reported separately.
`--orchestrator` remains a command-line alias for `--analysis-root`.

Full-family reports now include the final observed event. An inferred upper bound
is advanced by one microsecond; an explicit `--before` remains exclusive. Usage and
actions before a trusted child activation are excluded from primary totals and
counted in `pre_activation_records_excluded`. Existing carry-in exclusions still
apply. These changes can alter totals relative to v4.2; that is not evidence of
workflow savings. They do not prove arbitrary inherited/replayed history has been
identified when no reliable activation boundary exists.

#### v5.1 attribution correction

Role inference now uses only the leaf of this session's own task path in
`session_meta`. Ancestor names and message-sender/recipient paths cannot supply
self-role evidence. Exact role fields in that session's metadata are retained
separately from naming heuristics.

An unambiguous trusted assignment label or explicit mapping takes precedence over
weaker inferred naming evidence. The nested report preserves `inferred_role`,
`inferred_role_confidence`, `explicit_self_roles` and nonblocking `role_diagnostics`;
these describe responsibility evidence, not the actual activity of every request.
Conflicting explicit role declarations, labels, assignments or parents still block
unit attribution. No assignment is invented from a generic task name.

Correcting roles can change every role-based view, including recognized active
windows and concurrency. Compare the same topology level and reporting window:
family-wide descendant concurrency is not bounded-orchestrator concurrency. Raw
usage totals should remain unchanged for identical source records and boundaries.
This correction does not improve missing recipient attribution or prove savings.

### Tool-compatible assignment identity

The audit parser supports this versioned label convention:

```text
si1_<UTF-8 hex run ID>_<c or g>_<UTF-8 hex unit ID>_<role>_<attempt>
```

`c` identifies a chunk and `g` an explicitly named group. Hex uses lowercase digits
and preserves original case/punctuation without slug collisions. Structured-label
roles use configured lowercase ASCII letters (`a`–`z`) only; attempts are positive
decimal integers without leading zeroes.
Run/unit IDs are nonempty, at most 96 UTF-8 bytes each, without control characters;
the complete label is at most 512 characters. Respect any stricter tool limits and
use an explicit mapping if it cannot fit. Same-worker repair/revalidation retains
the assignment; a fresh replacement increments the attempt. A recorded parent
distinguishes the same role/attempt under different bounded leads.

Generate a label without maintaining another agent report:

```bash
python3 -c 'from workflow_attribution import encode_label; print(encode_label("O-03", "implementer", 1, run_id="run-a"))'
```

The corresponding logical identity is run `run-a`, assignment `O-03:implementer:1`.
Legacy colon labels remain readable but lack explicit run identity (reported as
null); they are scoped to the selected family and cannot prove separation of
multiple runs within that family. Arbitrary slugged names such as
`o_03_implementer_1` are **not decoded into assignments** (their role may still be
recognized through the separate metadata naming rule). Labels and unit IDs are not exported;
the report uses local `R01`/`U01` labels. Parent/session keys are one-way hashes.

Use labels actually recorded by the workflow, or supply its existing
assignment-to-worker mapping through the optional import below. Support for this
convention does not imply that an older run emitted these labels.

### Optional mapping import

Use `--assignment-map mapping.json` only when explicit identity information is
available from the existing handoff. This is an audit-side adapter, not a mandatory
second workflow ledger. Use hashed family/session keys from the finder or lifecycle
export. The map is validated against the selected source family, with no cross-family
joins. Conflicts with explicit self-role declarations, labels or parents are
reported and withheld from unit attribution rather than silently overwritten.
Disagreement with weaker inferred naming evidence is retained as a diagnostic,
without discarding an otherwise unambiguous explicit assignment.

```json
{
  "schema": "workflow-assignment-map-v1",
  "family": "W-...",
  "assignments": [
    {"session": "S-root...", "role": "coordinator"},
    {
      "session": "S-worker...",
      "parent_session": "S-parent...",
      "run_id": "run-a",
      "unit_id": "O-03",
      "scope": "chunk",
      "role": "implementer",
      "attempt": 1,
      "completed_at": "2026-09-20T12:00:00Z"
    }
  ]
}
```

`completed_at` is optional and must denote an explicitly recorded assignment end,
not the last observed request. Later usage is exposure requiring interpretation,
not automatically waste. An entry without `unit_id` can declare a role/parent only.
One session reused for different assignments cannot be split from this map: duplicate
session entries are rejected, and conflicting observed assignments are held out.
Same-assignment repair tokens remain combined unless further evidence distinguishes
their boundaries. Declared mapping evidence is labeled separately from observed
spawn/metadata evidence. No prompts, source contents or raw tool output belong here.

### Coordination and tool observations

The nested report includes each caller's lifecycle counts and target-resolution
coverage, including `followup_task` and `interrupt_agent`. An interrupt is not treated
as a close or proof that capacity was released. With the compaction scan enabled,
`tool_activity` reports structural call categories, duplicate removal, parse errors
and unclassified/opaque counts. `--no-compaction-audit` leaves that view null.

Custom tool calls are recognized. JavaScript `functions.exec` wrappers are explicitly
opaque: this scanner does not infer execution from code strings, comments or branches.
No tool category receives a fabricated share of response tokens. Tool counts cannot
establish unnecessary polling, duplicate investigation, successful tests or savings.
Resource-read detection remains limited by schema/classification coverage.

Validate the helpers with:

```bash
python3 find_workflow_candidates.py --self-test
python3 extract_workflow_lifecycle.py --self-test
python3 profile_workflow_cost.py --self-test
python3 -m unittest -v test_workflow_attribution test_generic_workflow test_workflow_pauses
```

Synthetic tests cover solo/flat/nested workflows, unknown/custom roles, unchanged
generic/staged accounting, incomplete linkage, independent cycle isolation,
duplicate snapshots, pre-activation history, cutoff handling, privacy, pause boundary
accounting, overlapping pauses, offline review and persistent decisions. Runtime
label/linkage emission still needs inspection on the logs being analyzed; these
tests do not establish compatibility with every Codex version or workflow savings.

### Earlier profiler methodology

This section records the earlier v4 methods. v6's structural root selection and
role-independent activity rules above supersede the historical role-based defaults.

v4 keeps v3's overlapping active-role model and adds two conservative evidence layers that are useful for before/after workflow experiments:

1. **restart-aware analysis windows** using timezone-aware `--after` / `--before` boundaries
2. **chunk / assignment correlation** only when a spawn exposes an explicit structured task label such as `<chunk-id>:<role>:<attempt>`

v4.1 fixed an important cutoff-reconstruction edge case found on real Codex logs. A resumed or forked rollout can retain inherited timestamps from before the point where that session was actually activated, so its recorded earliest timestamp is not a reliable restart boundary. v4.2 keeps that logic: with a cutoff, it selects the analysis orchestrator from **observed inference/lifecycle activity inside the requested window**, not from `first_ts` alone. Trusted spawn time is used as the effective activation time for child workers.

v4.2 adds an explicit **context-compaction audit**. It counts persisted compaction events directly, conservatively associates compaction-specific `token_count` samples when they are structurally close enough, measures observed context shrink/refill, and profiles the work performed while a session rebuilds context after compaction. It also tracks privacy-safe repeated resource reads/accesses during that recovery period and compares recovery requests with the same session's non-recovery requests as an exploratory baseline.

Without a cutoff, v4.2 retains the v3 behavior: the selected graph family root is the orchestrator and the full family is profiled. With a cutoff, it scores sessions that actually have post-boundary activity, with particular weight on CLI/root-like sessions and trusted recognized-role spawns. A session whose recorded start predates the cutoff can still be selected as the resumed/restarted orchestrator, but only its in-window events are counted. If selection is ambiguous, the profiler stops rather than silently choosing a root. An explicit privacy-safe override is available with `--orchestrator S-...`.

Example for a known workflow-update/restart boundary:

```bash
python3 profile_workflow_cost.py \
  --family W-ac4e5c3e8a \
  --after 2026-09-14T19:54:47+02:00 \
  --export-json workflow_cost_profile_v4_2.json
```

`--after` and `--before` require an explicit timezone offset. `--before` is exclusive.

### Window/restart handling

For a windowed run, sessions are audited as:

```text
pre-window
carry-in
in-window
post-window
carry-out
```

A **carry-in** session was activated before the boundary but still has observed activity afterward. For child workers, a trusted spawn timestamp overrides inherited rollout history when deciding whether the worker is carry-in or genuinely post-boundary. Other carry-in workers are reported separately and are **excluded from the primary post-restart totals by default**.

The selected root is the one allowed carry-in exception: if the same privacy-safe
session identity spans the restart boundary, only its in-window events are counted.
The report records structural root selection and activity evidence.

The selected segment is built from that root plus graph/trusted-spawn descendants
that meet the window rules. There is no fallback that adopts unrelated active
sessions when a descendant set cannot be established.

### Active-state and concurrency analysis

The base cost model still combines:

1. trusted spawn matches
2. observed child-session lifetime
3. optional role metadata for labeling, not admission

For descendants with trusted evidence, the active window begins at the **trusted spawn timestamp**, not the rollout's earliest timestamp. This avoids treating inherited/forked history as pre-spawn activity.

That produces overlapping **active-role windows**. If an implementer and validator are alive at the same time, both remain active. Each orchestrator inference request is assigned to one mutually exclusive state such as:

```text
implementer only
validator only
implementer+validator
multiple implementers
multiple implementers+validator
no-recognized-child-active
```

The profiler reports:

- total observed work and public API-list-price-equivalent work by role
- orchestrator/root cost by active child state
- root cost by 0 / 1 / 2 / 3+ observed concurrent descendants
- trusted lifetime windows and pairwise overlap, including unknown roles
- descendant agent-minutes, active wall time, overlap wall time, extra concurrency-hours, and peak simultaneous descendants
- exact role-level `SEND` targets when a routing field literally equals a configured role
- inference bursts and activity after the first burst
- per-session context growth, peak context, and apparent context resets
- large-context / small-output workload by role and orchestrator active state
- optionally configured role-pair cycles, with supervision ratios only for complete, isolated sequential cycles
- a privacy-safe action-schema audit

### Context compaction audit

v4.2 scans the selected rollout files for explicit persisted compaction markers, including paired `compacted` / `context_compacted` representations. Nearby representations are deduplicated into one observed compaction event.

This is intentionally separate from the older `resets` field in the context-growth table. That older field is only a **context-drop heuristic**: it counts a large request followed by a request below 55% of the prior input size. v4.2 reports explicit compactions directly and also shows how many heuristic drops matched an explicit compaction, plus explicit-only and heuristic-only events.

For each explicit compaction, v4.2 can report:

```text
context/request input immediately before compaction
input on the first ordinary inference request after compaction
observed shrink percentage
direct compaction token/API$eq usage when a raw usage sample can be safely matched
requests, tokens and API$eq during post-compaction recovery
time and requests until the context refills to the configured fraction
peak context reached during recovery
tool activity during recovery
resources accessed both shortly before and after compaction
repeated file-read events across the compaction boundary
```

Direct compaction token cost is **not forced**. Some rollouts emit a compaction-specific `last_token_usage` sample between the persisted compaction markers even when the cumulative token total does not advance; v4.2 reads raw token telemetry so it can recover that sample. If no sufficiently close usage record exists, the direct cost is reported as unobserved rather than assigning the next normal model request to compaction.

Because the normal workflow request tables deduplicate unchanged cumulative token totals, a matched compaction-specific usage sample can exist **outside** those primary request totals. v4.2 therefore reports how many matched direct-usage records were already present in the primary totals and separately reports matched direct API$eq that was outside them. Do not blindly add direct compaction API$eq to workflow totals without checking that field.

By default, a recovery window begins after the compaction markers and ends at the earliest of:

1. the next compaction in that session
2. the end of the session / requested analysis window
3. the first ordinary request whose input reaches 80% of the pre-compaction input

Tune the refill threshold with:

```bash
python3 profile_workflow_cost.py \
  --family W-... \
  --compaction-refill-fraction 0.8
```

To look for implementation-state reacquisition, the profiler also scans structural tool calls during recovery. It categorizes activity such as file reads, searches, Git/state inspection, tests/builds, writes/edits and other shell/tool work. Path-like resources are one-way hashed **locally**; raw paths, commands, prompts, source text and tool output are never printed or exported. The default pre-compaction resource lookback is 20 minutes:

```bash
python3 profile_workflow_cost.py \
  --family W-... \
  --compaction-resource-lookback-minutes 20
```

The report includes a **post-compaction recovery vs same-session non-recovery baseline**. It compares API$eq/request, input/request, uncached input/request and tool events/request. The aggregate delta is exploratory only. It means that recovery windows were more or less expensive than other requests in the same sessions; it does **not** prove that compaction caused the difference or that the delta is achievable savings.

Useful controls:

```bash
python3 profile_workflow_cost.py --family W-... --compaction-limit 50
python3 profile_workflow_cost.py --family W-... --compaction-direct-usage-seconds 1
python3 profile_workflow_cost.py --family W-... --compaction-dedupe-seconds 2
python3 profile_workflow_cost.py --family W-... --no-compaction-audit
```

The `--stage-roles` option keeps its name but now requests a **separate filtered
activity view** in `workflow_analysis.role_filtered_activity`. It does not filter
the main active windows, concurrency, cycle-isolation checks or token totals.

By default an active window ends at the observed end of the child session. An optional grace period can be added with:

```bash
python3 profile_workflow_cost.py \
  --family W-... \
  --active-tail-seconds 30
```

`--stage-tail-seconds` remains an alias.

### Chunk / assignment correlation

v4.2 can correlate implementer/validator/planner workers only from an explicit compact assignment label in a trusted spawn, for example:

```text
chunk-04:implementer:1
chunk-04:validator:1
chunk-04:implementer:2
chunk-05:implementer:1
```

The raw task label is never printed or exported. It is converted into report-local privacy-safe chunk labels such as `C01`, `C02`, etc.

Correlation is intentionally strict. Prose, timing proximity, or a role name by itself is **not** enough to invent a chunk identity. If the logs predate structured assignment labels, coverage may be zero and the report says so.

This lets v4.2 distinguish lifecycle overlap into:

```text
same-chunk-repair-overlap
same-chunk-replacement-overlap
cross-chunk-overlap
unclassified-overlap
```

Same-chunk implementer/validator overlap is therefore no longer treated as suspicious merely because validation began. **Cross-chunk active work** is the stronger lifecycle optimization signal. If chunk identity is unavailable, the overlap remains `unclassified-overlap` rather than being guessed from timing.

### Supervision ratios

For an isolated sequential `implementer -> validator` cycle, v4.2 can report:

```text
root work while implementer active / implementer work
root work while validator active / validator work
combined root stage work / combined direct child work
```

Ratios are suppressed for overlapping or truncated cycles, for sequential cycles
with another observed descendant active (including unknown roles), and for explicit
chunk mismatches. They are observational concurrency ratios, not guaranteed
avoidable overhead. v6 only computes these when a role pair is configured.

### Stable before/after comparison fields

The report and JSON export include a **Comparison snapshot** with stable descriptive fields intended for comparing workflow versions, including:

```text
root_api_eq_share
root_requests
root_median_input_tokens
root_p90_input_tokens
large_context_small_output_api_eq_share
root_with_2plus_children_api_eq_share
recognized_child_agent_hours
extra_concurrency_hours
peak_concurrent_children
chunk_correlation_coverage
correlated_chunks
direct_child_requests_per_correlated_chunk
direct_child_api_eq_per_correlated_chunk
cross_chunk_overlap_api_eq
same_chunk_repair_overlap_api_eq
same_chunk_replacement_overlap_api_eq
unclassified_overlap_api_eq
explicit_compactions
direct_compaction_usage_coverage
direct_compaction_api_eq
direct_compaction_api_eq_outside_primary_totals
post_compaction_recovery_api_eq
post_compaction_recovery_api_eq_share
post_compaction_recovery_input_tokens
post_compaction_recovery_requests
root_compactions
root_compactions_per_hour
repeated_post_compaction_read_events
recovery_api_eq_delta_vs_same_session_baseline
```

These are descriptive measurements, not causal estimates of savings. The overlap classes can overlap conceptually and must not be summed into a savings number.

### Exact role-targeted sends

When a safe routing field exactly equals a configured role, such as:

```text
send.args.target = validator
```

v4.2 trusts the **role-level target**. It still does not claim to know the specific child session unless an actual session-ID bridge exists. Any nearby root inference pairing remains a timing heuristic and is labeled observational.

### Custom workflows

All roles, including unknown roles, contribute to core accounting and trusted
lifetime windows. The default recognition vocabulary is `coordinator`,
`orchestrator`, `planner`, `implementer`, `validator`; this is only a labeling aid.
To recognize other explicit role names and optionally study a role pair:

```bash
python3 profile_workflow_cost.py \
  --family W-... \
  --roles manager coder reviewer \
  --stage-roles coder reviewer \
  --cycle-roles coder reviewer \
  --successor coder=reviewer
```

`--roles` replaces the vocabulary. Role names must match explicit metadata/labels;
the profiler does not infer “coder” from arbitrary prompt text. Omit `--stage-roles`,
`--cycle-roles` and `--successor` if you only want neutral session/tree accounting.

Only `--workflow-profile staged` defaults to implementer → validator cycle analysis
and these successor relations:

```text
planner -> implementer
implementer -> validator
validator -> implementer
```

Generic mode has no default successor map. Repeatable `--successor` options add or
override relationships using configured role names. With a custom `--roles` list,
either stay in generic mode or supply a compatible `--cycle-roles` pair to override
the staged default. Observed role succession is not proof of repair, acceptance or
unnecessary work. Per-unit attribution still requires explicit assignment evidence.

#### Privacy and interpretation

The workflow helpers remain read-only and local. They do not print prompts, responses, source code, tool output, or raw session/thread/agent IDs.

Useful options:

```bash
python3 profile_workflow_cost.py --help
python3 profile_workflow_cost.py --self-test
python3 profile_workflow_cost.py --family W-... --no-action-schema-audit
python3 profile_workflow_cost.py --family W-... --prices prices.json
python3 profile_workflow_cost.py --family W-... --after 2026-09-14T19:54:47+02:00
python3 profile_workflow_cost.py --family W-... --after 2026-09-14T19:54:47+02:00 --orchestrator S-...
```

`API$eq` uses the same public list-price table as the main audit. It is a normalization ruler only, not a Codex subscription charge or OpenAI internal compute cost. Guardian long-context normalization uses the same mapping and multipliers as `codex_quota_audit.py`.

The action-schema audit never prints argument/result values. It only prints structural paths, data types, counts, and whether hashed compact values overlap known family IDs or trusted spawn-result value namespaces. Any proposed handle bridge remains diagnostic until validated on real workflows.

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

The workflow helper outputs use hashed/stable labels and avoid printing prompts, responses, source code, tool stdout, or raw linkage IDs.


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
- Workflow lifecycle attribution is conservative: only trusted exact-ID or very-tight spawn/start matches create follow-up phases or role-targeted supervision totals.
- Diagnostic `single-active-agent` lifecycle hints are never used for cost attribution.
- Workflow profiler active-state attribution can show multiple recognized children at once; per-window root totals can therefore overlap and should not be summed.
- Exact role-targeted `SEND` labels are trusted only when a routing field exactly equals a configured role name. Nearby inference pairing remains observational.
- Supervision ratios are emitted only for isolated sequential cycles. Ratios are suppressed for overlapping or non-isolated cycles and remain observational concurrency measures, not guaranteed avoidable overhead.
- Lingering-agent candidates mean only that an older recognized child remains alive after a later same-role or configured successor-role spawn. Parallelism may be intentional; post-trigger inference is reported as observed exposure, not savings.

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

Experimental workflow helper versions in this package:

```text
find_workflow_candidates.py      2.3
extract_workflow_lifecycle.py    2.3
profile_workflow_cost.py         6.1
```

---

## Disclaimer

This project is an independent analysis tool for locally recorded Codex telemetry.

Results should be treated as empirical evidence from the available logs, not as authoritative documentation of Codex quota policy or OpenAI's internal accounting.
