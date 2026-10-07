# Performance baseline

Performance is workload- and machine-dependent, so Codex Quota Audit does not publish a universal speed claim. The release benchmark exists to make local regressions visible.

Run:

```bash
python3 tools/benchmark_release.py --repeat 5
```

## 0.5.0 development baseline

On the build environment used for the 0.5.0 development release, the synthetic all-sections report fixture averaged approximately:

| Measurement | Average |
| --- | ---: |
| Report-model construction | 0.042 s |
| Construction + JSON + HTML rendering | 0.054 s |
| Peak traced Python memory | 1.67 MB |
| Report JSON size | 114,771 bytes |
| Self-contained HTML size | 136,396 bytes |

This fixture exercises the presentation/report pipeline with quota, Guardian, banked-reset, and workflow data. It does **not** represent the cost of scanning a large live Codex history. For workflow indexing measurements, use the separate benchmark below.


## Workflow indexing and cached analysis

CQA owns a SQLite cache containing file signatures, session/link metadata and extracted observations. It does not depend on Codex's internal state database. Detailed workflow telemetry is extracted in one streaming pass per selected file; consumers share lazy JSON decoding and lowercasing. Quota observations are extracted alongside workflow discovery so combined dashboards can reuse them.

Version 0.9.1 adds privacy-safe authentication facts to cached observations and increments the extraction format. Existing entries are refreshed automatically on their next use, so the first run after upgrading may re-index logs. Auto-review policy classification and local sign-in declarations are applied after extraction; changing a declaration does not require rebuilding the cache.

Version 0.10.0 introduces independent extraction versions for discovery, quota, detailed telemetry and calendar usage. Existing v3 discovery/quota/telemetry keys remain valid. `cqa usage` adds v1 usage entries to the same database; its initial pass includes valid observations without rate-limit snapshots, so the older meter-backed cache cannot establish complete usage coverage. Later usage queries reuse unchanged entries regardless of calendar range, model filter, timezone or pricing. The command refreshes files and loads cached observations without building the workflow graph or running quota fits. Its `--rebuild-cache` clears only usage entries. Date-range SQL indexing and appended-tail parsing remain separate future optimizations; this implementation loads available cached observations before replay classification and calendar selection.

The first index build reads the complete history to preserve relationship coverage. Subsequent runs list/stat files and parse only new or changed files. `latest` passes its discovery result to the profiler. Cached timestamps narrow recent candidate selection while retaining each selected family's older ancestors and workers. The selected workflow's unchanged detailed observations are loaded from SQLite, and report calculations run again with current prices, windows and thresholds.

File signatures include device/inode identity, size, modification time and change time. Replaced, truncated, appended or moved files are reparsed in full. Deleted files are removed from the index. Cache schema and extraction versions are independent of the portable report schema; changing extraction semantics or role-recognition vocabulary invalidates affected entries. A file changed during extraction is not saved as an up-to-date cache entry. Completed files are committed independently, so an interrupted build can resume.

The cache retains content-free structural evidence, counts, timestamps and resource fingerprints. File paths and compact task labels are local metadata; the database is not a portable report. It does not retain conversation messages or raw tool output. JSON encodes explicit dataclass/container types; cache loading does not execute pickle data. Unavailable caches and malformed entries fall back to extraction from logs. `--no-cache` bypasses cache reads/writes, and `--rebuild-cache` clears cached entries for the selected Codex home. SQLite uses WAL with NORMAL synchronization for reconstructible cache data; source logs remain authoritative if a power loss discards recent cache writes.

Parallel indexing is optional (`--workers 2`, up to 16). Work is bounded to two files per worker per batch; workers extract data and the parent alone writes SQLite. The default remains one worker because process overhead and disk contention depend on the workload. Reading only appended tails is deferred until resumable parser state and incomplete records can preserve the same metrics; current updates reparse the changed file.

Run a reproducible benchmark without reading your real Codex history:

```bash
python3 tools/benchmark_workflow.py --files 256 --payload-kib 256
python3 tools/benchmark_workflow.py --files 256 --payload-kib 256 --workers 2
```

The fixture generates temporary logs and compares uncached discovery, an initial cache build, and a warm run. It prints elapsed seconds, cache hits, processed files and log bytes read, then removes its temporary files. These measurements cover indexing, not complete report generation. For a real workflow, `cqa workflow profile YOUR_SESSION_ID --timings` shows stage durations and cache/file counters in the progress output.
