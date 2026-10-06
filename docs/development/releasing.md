# Releasing Codex Quota Audit

Current package/plugin version: **0.9.0**.

The package/plugin surface follows Semantic Versioning. Internal quota/workflow
analyzer versions remain independent for research reproducibility.

## Release gate

Run from a clean checkout:

```bash
python3 tools/release_check.py
```

It verifies:

- all unit/integration/regression tests;
- canonical/public/plugin mirror synchronization;
- quota/workflow/candidate-finder self-tests;
- Python compilation;
- plugin launchers;
- privacy-canary report generation;
- offline wheel construction and package contents;
- wheel import/install and console/research smokes;
- `git diff --check` when Git metadata is present.

Every subprocess stage is named, flushed, time-bounded, and prints captured
stdout/stderr on failure.
Bytecode compilation and wheel build metadata are directed into temporary
release-check storage so a successful gate does not leave `build/`, `*.egg-info`,
or `__pycache__` debris in the checkout.

## Release archive

Release archives are built from a clean clone of the exact committed HEAD, not
from the development directory:

```bash
python3 tools/build_release.py --output ../CodexQuotaAudit_0.9.0_with_git.zip
```

The archive includes `.git` history intentionally. Because the tool refuses a
dirty source tree and clones only committed state, local previews, reports,
screenshots, research exports, and scratch files cannot leak into a release.
The temporary clone drops its machine-local `origin` URL before packaging, so
the archive does not retain the maintainer's local checkout path.

## Version changes

Update together:

- `pyproject.toml` project version;
- `src/cqa/__init__.py`;
- `src/cqa/cli.py` display version;
- `src/cqa/research/throughput_compare.py` generator version when appropriate;
- `plugins/codex-quota-audit/.codex-plugin/plugin.json`;
- this document.

Then run `tools/sync_plugin_runtime.py`.

Do not change frozen `cqa-report-v1.0` semantics in a compatible report release.

## Manual release-candidate checks

Before public tagging:

1. Real Codex plugin marketplace/add/new-thread smoke.
2. One real quota report and one real workflow report.
3. Browser sanity on Chromium plus Firefox/Safari/Edge where available.
4. Keyboard/focus/zoom/narrow-screen accessibility check.
5. Confirm `git status --short` is empty after the full release process.
