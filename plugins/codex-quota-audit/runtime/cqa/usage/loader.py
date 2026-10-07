"""Shared cached usage loading, deduplication and replay detection.

The legacy quota CLI uses the meter-backed projection. Calendar usage retains
valid token observations regardless of meter availability. Prices and selection
windows are applied after extraction, never frozen into cached observations.
"""
from __future__ import annotations

import sys
from dataclasses import fields
from typing import Dict, List, Tuple
from ..quota import audit
from ..workflow.cache import Index


def load_events(home: str,
                prices: Dict[str, Tuple[float, float, float]],
                target_window_minutes: int,
                replay_scan_seconds: float,
                replay_min_events: int,
                replay_min_growth_mtokens: float,
                replay_start_max_mtokens: float,
                dense_threshold: int, *, use_cache=True, cache_dir=None, rebuild_cache=False,
                include_unmetered=False, index_factory=None, progress=None) -> Tuple[List[audit.Event], audit.ParseStats]:
    """Return deduplicated events with evidence-based replay flags attached."""
    stats = audit.ParseStats()
    stats.unreadable_files = 0
    cache_kind = "usage" if include_unmetered else "quota"
    index_factory = index_factory or Index
    paths = audit.session_files(home)
    stats.files = len(paths)

    candidates: List[audit.Event] = []
    rebuild_options = {"rebuild_kind": "usage"} if include_unmetered else {}
    with index_factory(home, cache_dir, enabled=use_cache, rebuild=rebuild_cache, **rebuild_options) as index:
        index.prune(paths)
        if progress:
            progress("scan_start", total=len(paths))
        for position, path in enumerate(paths, 1):
            try:
                cached, before = index.get(path, cache_kind, target_window_minutes)
            except OSError:
                stats.unreadable_files += 1
                continue
            if cached is None:
                local_stats = audit.ParseStats()
                try:
                    options = {"include_unmetered": True} if include_unmetered else {}
                    events = audit.parse_file(path, {}, target_window_minutes, local_stats, **options)
                except OSError:
                    stats.unreadable_files += 1
                    continue
                cached = (events, local_stats)
                index.put(path, cache_kind, target_window_minutes, before, cached)
            events, local_stats = cached
            for item in fields(audit.ParseStats):
                value = getattr(local_stats, item.name)
                target = getattr(stats, item.name)
                if isinstance(value, int):
                    setattr(stats, item.name, target + value)
                elif item.name in {"window_records", "link_schema_paths"}:
                    for key, count in value.items():
                        target[key] = target.get(key, 0) + count
                else:
                    target.update(value)
            for event in events:
                event.api_usd = audit.price_event(event.model, event.uncached, event.cached, event.output, prices, ts=event.ts)
                event.priced = event.api_usd is not None
            candidates.extend(events)
            if progress and position % 250 == 0:
                progress("scan_progress", current=position, total=len(paths), **index.stats())
        stats.cache_stats = index.stats()
        stats.cache_warning = index.warning
        if index.warning:
            print(index.warning, file=sys.stderr)
        if progress:
            progress("scan_progress", current=len(paths), total=len(paths), **index.stats())
    candidates.sort(key=lambda e: (e.ts, e.ts_raw, e.source))

    by_identity: Dict[Tuple[object, ...], audit.Event] = {}
    for e in candidates:
        ident = audit.event_identity(e)
        prev = by_identity.get(ident)
        if prev is None:
            by_identity[ident] = e
        else:
            stats.global_duplicates += 1
            prev_quality = (
                int(prev.model != "unknown") + int(prev.effort != "unknown")
                + int(prev.approvals_reviewer != "unknown")
                + int(prev.approval_policy != "unknown")
                + int(prev.source_kind != "unknown")
            )
            new_quality = (
                int(e.model != "unknown") + int(e.effort != "unknown")
                + int(e.approvals_reviewer != "unknown")
                + int(e.approval_policy != "unknown")
                + int(e.source_kind != "unknown")
            )
            if new_quality > prev_quality:
                by_identity[ident] = e

    deduped = sorted(by_identity.values(), key=lambda e: (e.ts, e.ts_raw, e.source))
    audit.detect_replay_prefixes(
        deduped, stats, replay_scan_seconds, replay_min_events,
        replay_min_growth_mtokens, replay_start_max_mtokens, dense_threshold,
    )
    return deduped, stats
