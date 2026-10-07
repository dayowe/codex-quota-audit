"""Local report-library helpers for Codex Quota Audit.

The catalog is intentionally local application state. Portable CQA reports do
not contain raw Codex session/thread IDs. The catalog may retain the selector
that was used to produce a workflow report so a local power user can correlate
an artifact back to the source workflow without leaking that identity when the
HTML is shared.
"""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

CATALOG_VERSION = 1


def root(home: str) -> Path:
    return Path(os.path.expanduser(home)).resolve() / "codex-quota-audit"


def catalog_path(home: str) -> Path:
    return root(home) / "catalog.json"


def index_path(home: str) -> Path:
    return root(home) / "index.html"


def reports_dir(home: str) -> Path:
    return root(home) / "reports"


def json_dir(home: str) -> Path:
    return root(home) / "json"


def latest_dir(home: str) -> Path:
    return root(home) / "latest"


def ensure_dirs(home: str) -> None:
    for p in (root(home), reports_dir(home), json_dir(home), latest_dir(home)):
        p.mkdir(parents=True, exist_ok=True)


def load_catalog(home: str) -> dict:
    path = catalog_path(home)
    if not path.exists():
        return {"version": CATALOG_VERSION, "reports": []}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": CATALOG_VERSION, "reports": []}
    if not isinstance(obj, dict) or not isinstance(obj.get("reports"), list):
        return {"version": CATALOG_VERSION, "reports": []}
    obj["version"] = CATALOG_VERSION
    return obj


def save_catalog(home: str, catalog: Mapping[str, object]) -> None:
    ensure_dirs(home)
    path = catalog_path(home)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def slugify(value: str, limit: int = 48) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-+", "-", text)
    return (text[:limit].rstrip("-") or "report")


def _parse_dt(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_stamp(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def _workflow_ref(source_selector: Optional[str], report: Mapping[str, object]) -> Optional[str]:
    # W-* family IDs are already one-way privacy-safe workflow identifiers.
    if source_selector and str(source_selector).startswith("W-"):
        return "wf-" + str(source_selector)[2:10].lower()
    workflow = report.get("workflow")
    profiles = workflow.get("profiles") if isinstance(workflow, Mapping) else None
    if isinstance(profiles, Sequence) and profiles:
        ext = profiles[0].get("extensions") if isinstance(profiles[0], Mapping) else None
        source_family = ext.get("source_family") if isinstance(ext, Mapping) else None
        if isinstance(source_family, str) and source_family.startswith("W-"):
            return "wf-" + source_family[2:10].lower()
    return None


def report_type(report: Mapping[str, object]) -> str:
    quota = report.get("quota")
    workflow = report.get("workflow")
    q = isinstance(quota, Mapping) and quota.get("status") not in {"not_requested", "not_available"}
    w = isinstance(workflow, Mapping) and bool(workflow.get("profiles"))
    if q and w:
        return "combined"
    if w:
        return "workflow"
    info = report.get("report")
    extensions = info.get("extensions") if isinstance(info, Mapping) else None
    if not q and isinstance(extensions, Mapping) and isinstance(extensions.get("usage"), Mapping):
        return "usage"
    return "quota"


def _analysis_range(report: Mapping[str, object], rtype: str) -> tuple[Optional[str], Optional[str]]:
    if rtype == "usage":
        period = report["report"]["extensions"]["usage"]["period"]
        return period["start_inclusive"], period["end_exclusive"]
    if rtype in {"workflow", "combined"}:
        wf = report.get("workflow")
        profiles = wf.get("profiles") if isinstance(wf, Mapping) else None
        if isinstance(profiles, Sequence) and profiles:
            win = profiles[0].get("analysis_window") if isinstance(profiles[0], Mapping) else None
            if isinstance(win, Mapping):
                return win.get("start"), win.get("end")
    info = report.get("report")
    observed = info.get("observed_range") if isinstance(info, Mapping) else None
    if isinstance(observed, Mapping):
        return observed.get("start"), observed.get("end")
    return None, None


def _summary(report: Mapping[str, object], rtype: str) -> dict:
    result = {}
    if rtype == "usage":
        usage = report["report"]["extensions"]["usage"]
        for key in ("requests", "total_tokens", "api_list_equivalent_usd", "priced_subtotal_usd", "pricing_status"):
            result[key] = usage["summary"][key]
        result["period_label"] = usage["period"]["label"]
        result["timezone"] = usage["period"]["timezone"]
        result["calendar_start"] = usage["period"]["calendar_start"]
        result["calendar_end"] = usage["period"]["calendar_end"]
    if rtype in {"workflow", "combined"}:
        wf = report.get("workflow")
        profiles = wf.get("profiles") if isinstance(wf, Mapping) else None
        if isinstance(profiles, Sequence) and profiles and isinstance(profiles[0], Mapping):
            s = profiles[0].get("summary")
            if isinstance(s, Mapping):
                for key in ("sessions", "requests", "raw_tokens", "compactions", "peak_concurrent_children"):
                    if key in s:
                        result[key] = s[key]
    if rtype in {"quota", "combined"}:
        quota = report.get("quota")
        if isinstance(quota, Mapping):
            result["quota_cohorts"] = len(quota.get("cohorts") or [])
        guardian = report.get("guardian")
        result["guardian"] = isinstance(guardian, Mapping) and guardian.get("status") == "complete"
        banked = report.get("banked_resets")
        result["banked_resets"] = isinstance(banked, Mapping) and banked.get("status") == "complete"
    return result


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    i = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{i:02d}{suffix}"
        i += 1
    return candidate


def register_report(home: str, html_source: str | Path, report: Mapping[str, object], *,
                    json_source: str | Path | None = None,
                    friendly_name: str | None = None,
                    source_selector: str | None = None,
                    source_session_hint: str | None = None) -> dict:
    """Archive one generated report and update the local report library."""
    ensure_dirs(home)
    rtype = report_type(report)
    info = report.get("report") if isinstance(report.get("report"), Mapping) else {}
    generated = _parse_dt(info.get("generated_at")) or datetime.now(timezone.utc)
    start, end = _analysis_range(report, rtype)
    workflow_ref = _workflow_ref(source_selector, report)

    if rtype == "usage":
        period = report["report"]["extensions"]["usage"]["period"]
        date_part = slugify(period["label"] if period["label"] != "timestamp range" else period["calendar_start"][:10])
    elif rtype == "workflow" and start:
        basis = _parse_dt(start) or generated
        date_part = basis.astimezone().strftime("%Y-%m-%d")
    elif rtype == "combined" and start:
        basis = _parse_dt(start) or generated
        date_part = basis.astimezone().strftime("%Y-%m-%d")
    else:
        date_part = _local_stamp(generated)

    bits = [date_part]
    if friendly_name:
        bits.append(slugify(friendly_name))
    if workflow_ref:
        bits.append(workflow_ref)
    if not workflow_ref or rtype == "combined":
        bits.append(rtype)
    stem = "_".join(bits)
    html_dest = _unique_path(reports_dir(home), stem, ".html")
    shutil.copy2(Path(html_source), html_dest)

    json_dest = None
    if json_source is not None and Path(json_source).exists():
        json_dest = _unique_path(json_dir(home), html_dest.stem, ".json")
        shutil.copy2(Path(json_source), json_dest)

    # Stable latest copies are ordinary files, not symlinks, for Windows/browser portability.
    latest_html = latest_dir(home) / f"{rtype}.html"
    shutil.copy2(html_dest, latest_html)
    latest_json = latest_dir(home) / f"{rtype}.json"
    if json_dest is not None:
        shutil.copy2(json_dest, latest_json)
    elif latest_json.exists():
        # Do not leave a stale JSON pointer from an older report when the
        # newest report was generated without JSON export.
        latest_json.unlink()

    cat = load_catalog(home)
    existing = cat.get("reports") if isinstance(cat.get("reports"), list) else []
    digest = hashlib.sha256(html_dest.name.encode("utf-8")).hexdigest()[:6]
    rid = f"rpt-{generated.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')}-{digest}"
    entry = {
        "id": rid,
        "type": rtype,
        "name": friendly_name.strip() if friendly_name else None,
        "workflow_ref": workflow_ref,
        "generated_at": generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "analysis_start": start,
        "analysis_end": end,
        "html": str(html_dest.relative_to(root(home))),
        "json": str(json_dest.relative_to(root(home))) if json_dest is not None else None,
        "source_selector": source_selector,
        "source_session_hint": source_session_hint,
        "summary": _summary(report, rtype),
    }
    # Insert first so reports generated at the same timestamp retain creation
    # order with the newest artifact first after the stable timestamp sort.
    existing.insert(0, entry)
    existing.sort(key=lambda e: str(e.get("generated_at") or ""), reverse=True)
    cat = {"version": CATALOG_VERSION, "reports": existing}
    save_catalog(home, cat)
    write_index(home, cat)
    return entry


def _fmt_dt(value: object, *, date_only: bool = False) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return "—"
    local = dt.astimezone()
    return local.strftime("%b %d, %Y" if date_only else "%b %d, %Y %H:%M")


def _fmt_range(entry: Mapping[str, object]) -> str:
    if entry.get("type") == "usage":
        summary = entry.get("summary") or {}
        label = summary.get("period_label", "Usage")
        if label == "timestamp range":
            label = f"{summary.get('calendar_start', '—')} ≤ timestamp < {summary.get('calendar_end', '—')}"
        return f"{label} · {summary.get('timezone', 'UTC')}"
    start = _parse_dt(entry.get("analysis_start"))
    end = _parse_dt(entry.get("analysis_end"))
    if not start and not end:
        return "No analysis range"
    if start and end:
        sl, el = start.astimezone(), end.astimezone()
        if sl.date() == el.date():
            return f"{sl.strftime('%b %d %H:%M')} → {el.strftime('%H:%M')}"
        return f"{sl.strftime('%b %d')} → {el.strftime('%b %d')}"
    return _fmt_dt(start or end)


def _summary_text(entry: Mapping[str, object]) -> str:
    s = entry.get("summary") if isinstance(entry.get("summary"), Mapping) else {}
    parts = []
    if "sessions" in s:
        parts.append(f"{int(s['sessions']):,} sessions")
    if "requests" in s:
        parts.append(f"{int(s['requests']):,} {'usage records' if entry.get('type') == 'usage' else 'turns'}")
    if "total_tokens" in s:
        parts.append(f"{int(s['total_tokens']):,} tokens")
        status = s.get("pricing_status")
        if status in {"complete", "partial"}:
            value = float(s["priced_subtotal_usd"])
            dollars = "<$0.01" if 0 < value < .01 else f"${value:,.2f}"
            parts.append(dollars + (" priced subtotal" if status == "partial" else " API equivalent"))
        else:
            parts.append("API equivalent unavailable")
    if "raw_tokens" in s:
        n = float(s["raw_tokens"])
        parts.append(f"{n/1e6:.1f}M tokens" if n < 1e9 else f"{n/1e9:.2f}B tokens")
    if "quota_cohorts" in s:
        parts.append(f"{int(s['quota_cohorts'])} quota cohorts")
    if s.get("guardian"):
        parts.append("Guardian")
    if s.get("banked_resets"):
        parts.append("banked resets")
    return " · ".join(parts) or "Local CQA report"


def write_index(home: str, catalog: Mapping[str, object] | None = None) -> Path:
    ensure_dirs(home)
    cat = dict(catalog or load_catalog(home))
    rows = cat.get("reports") if isinstance(cat.get("reports"), list) else []
    cards = []
    for e in rows:
        if not isinstance(e, Mapping):
            continue
        href = html.escape(str(e.get("html") or ""), quote=True)
        default_label = {"workflow": "Workflow report", "combined": "Combined report", "usage": "Usage report"}.get(e.get("type"), "Quota audit")
        label = html.escape(str(e.get("name") or default_label))
        typ = html.escape(str(e.get("type") or "report").title())
        wref = f" · {html.escape(str(e.get('workflow_ref')))}" if e.get("workflow_ref") else ""
        cards.append(f'''<a class="card" href="{href}"><div class="title">{label}</div><div class="meta">{typ}{wref}</div><div class="range">{html.escape(_fmt_range(e))}</div><div class="summary">{html.escape(_summary_text(e))}</div><div class="generated">Generated {_fmt_dt(e.get('generated_at'))}</div></a>''')
    empty = '<div class="empty">No reports yet. Run <code>cqa dashboard</code>, <code>cqa workflow profile latest</code>, or <code>cqa usage --dashboard</code>.</div>'
    doc = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex Quota Audit · Reports</title><style>
:root{{--bg:#090d12;--panel:#101720;--border:#202b37;--text:#e7edf4;--muted:#7f8c9a;--blue:#9bb9d5}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}}main{{max-width:1000px;margin:auto;padding:42px 24px 80px}}h1{{margin:0;font-size:30px;letter-spacing:-.03em}}p{{color:var(--muted);margin:6px 0 28px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}.card{{display:block;text-decoration:none;color:inherit;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:17px;transition:.15s}}.card:hover,.card:focus-visible{{border-color:#39516a;transform:translateY(-1px);outline:none}}.title{{font-size:15px;font-weight:650}}.meta{{font-size:10px;color:var(--blue);margin-top:3px;text-transform:uppercase;letter-spacing:.07em}}.range{{font-size:12px;margin-top:15px}}.summary,.generated{{font-size:11px;color:var(--muted);margin-top:4px}}.empty{{border:1px dashed var(--border);border-radius:12px;padding:28px;color:var(--muted)}}code{{color:var(--blue)}}
</style></head><body><main><h1>Codex Quota Audit</h1><p>Local report library · newest first · report contents stay on this machine.</p><div class="grid">{''.join(cards) if cards else empty}</div></main></body></html>'''
    path = index_path(home)
    path.write_text(doc, encoding="utf-8")
    return path


def list_entries(home: str, *, report_type_filter: str | None = None) -> list[dict]:
    rows = load_catalog(home).get("reports") or []
    out = [dict(x) for x in rows if isinstance(x, Mapping)]
    if report_type_filter:
        out = [x for x in out if x.get("type") == report_type_filter]
    return out


def resolve_entry(home: str, selector: str) -> Optional[dict]:
    rows = list_entries(home)
    s = selector.strip()
    aliases = {
        "latest": None,
        "latest-quota": "quota",
        "latest-workflow": "workflow",
        "latest-combined": "combined",
        "latest-usage": "usage",
    }
    if s in aliases:
        want = aliases[s]
        return next((e for e in rows if want is None or e.get("type") == want), None)
    exact = next((e for e in rows if e.get("id") == s), None)
    if exact:
        return exact
    # Friendly-name lookup is intentionally convenience-only; users are never expected to remember names.
    matches = [e for e in rows if str(e.get("name") or "").casefold() == s.casefold()]
    return matches[0] if len(matches) == 1 else None
