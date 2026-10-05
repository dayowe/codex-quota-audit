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

This fixture exercises the presentation/report pipeline with quota, Guardian, banked-reset, and workflow data. It does **not** represent the cost of scanning a large live Codex history. Full-history scan benchmarks are still a release-roadmap item and should be measured on representative small/medium/large histories before optimizing the current repeated scans used by `--workflow latest`.
