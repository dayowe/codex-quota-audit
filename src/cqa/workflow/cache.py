"""Rebuildable local SQLite index of extracted workflow observations.

The cache contains parser results, never rollout JSON or conversation content.
Logs remain authoritative. Each entry is qualified by file identity and parser
configuration, and completed files are committed separately for safe interruption.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Bump when extraction semantics or cached dataclass layouts change.
EXTRACTION_VERSION = 2
_MODULES = {"cqa.workflow.candidates", "cqa.workflow.lifecycle",
            "cqa.workflow.profile", "cqa.workflow.telemetry", "cqa.quota.audit"}


def encode(value):
    if dataclasses.is_dataclass(value):
        module = value.__class__.__module__
        if module == "__main__":
            spec = getattr(sys.modules["__main__"], "__spec__", None)
            module = spec.name if spec else module
        return {"@": "class", "type": module + ":" + value.__class__.__name__,
                "fields": {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, datetime):
        return {"@": "datetime", "value": value.isoformat()}
    if isinstance(value, (dict, Counter, defaultdict)):
        kind = "counter" if isinstance(value, Counter) else "dict"
        if isinstance(value, defaultdict):
            kind = "default:" + value.default_factory.__name__
        return {"@": kind, "items": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (set, frozenset, tuple)):
        return {"@": type(value).__name__, "items": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported cache value: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value["@"]
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "class":
        module, name = value["type"].split(":")
        if module not in _MODULES or name.startswith("_"):
            raise ValueError("Unknown cached type")
        cls = getattr(importlib.import_module(module), name)
        if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
            raise ValueError("Unknown cached type")
        return cls(**{k: decode(v) for k, v in value["fields"].items()})
    items = value["items"]
    if kind in {"set", "frozenset", "tuple"}:
        return {"set": set, "frozenset": frozenset, "tuple": tuple}[kind](decode(v) for v in items)
    pairs = [(decode(k), decode(v)) for k, v in items]
    if kind.startswith("default:"):
        factory = {"set": set, "list": list, "int": int, "dict": dict, "Counter": Counter}[kind[8:]]
        return defaultdict(factory, pairs)
    if kind == "counter":
        return Counter(dict(pairs))
    if kind != "dict":
        raise ValueError("Unknown cache container")
    return dict(pairs)


def signature(path):
    st = os.stat(path)
    return json.dumps([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])


class Index:
    def __init__(self, home, directory=None, *, enabled=True, rebuild=False):
        self.home = str(Path(home).expanduser().resolve())
        self.path = Path(directory).expanduser() / "workflow.sqlite3" if directory else Path(self.home) / "codex-quota-audit" / "cache" / "workflow.sqlite3"
        self.connection = None
        self.hits = self.misses = self.bytes_read = 0
        self.started = time.monotonic()
        self.warning = None
        if not enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.connection = sqlite3.connect(self.path, timeout=2)
            schema_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version not in (0, 1):
                raise sqlite3.DatabaseError("Unsupported workflow cache schema")
            # Cache data is reconstructible. NORMAL in WAL mode avoids a disk
            # sync for every extracted file while keeping transactions atomic.
            journal = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if journal == "wal":
                self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    home TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
                    config TEXT NOT NULL, signature TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(home, path, kind, config));
                CREATE TABLE IF NOT EXISTS sessions (
                    home TEXT NOT NULL, path TEXT NOT NULL, config TEXT NOT NULL,
                    session_key TEXT NOT NULL, first_ts REAL, last_ts REAL,
                    PRIMARY KEY(home, path, config));
                CREATE INDEX IF NOT EXISTS sessions_recent ON sessions(home, config, last_ts);
                CREATE TABLE IF NOT EXISTS identifiers (
                    home TEXT NOT NULL, path TEXT NOT NULL, config TEXT NOT NULL,
                    identifier TEXT NOT NULL, is_parent INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS identifier_lookup ON identifiers(home, config, identifier);
            """)
            self.connection.execute("PRAGMA user_version=1")
            if rebuild:
                with self.connection:
                    for table in ("entries", "sessions", "identifiers"):
                        self.connection.execute(f"DELETE FROM {table} WHERE home=?", (self.home,))
        except (OSError, sqlite3.Error) as exc:
            self.warning = f"Workflow cache unavailable ({type(exc).__name__}); reading logs directly."
            self.close()

    @staticmethod
    def config(options):
        return json.dumps([EXTRACTION_VERSION, options], sort_keys=True, separators=(",", ":"))

    def get(self, path, kind, options):
        config = self.config(options)
        before = signature(path)
        if self.connection:
            try:
                row = self.connection.execute("SELECT signature,data FROM entries WHERE home=? AND path=? AND kind=? AND config=?",
                                              (self.home, path, kind, config)).fetchone()
                if row and row[0] == before:
                    result = decode(json.loads(row[1]))
                    expected = {"discovery": "Session", "telemetry": "Telemetry"}
                    if kind in expected and type(result).__name__ != expected[kind]:
                        raise ValueError("Unexpected cached record type")
                    if kind == "quota" and not (isinstance(result, tuple) and len(result) == 2
                                                and isinstance(result[0], list)
                                                and type(result[1]).__name__ == "ParseStats"):
                        raise ValueError("Unexpected cached quota record")
                    self.hits += 1
                    return result, before
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                # A damaged or obsolete entry is expendable; re-extract it.
                pass
        self.misses += 1
        self.bytes_read += os.stat(path).st_size
        return None, before

    def put(self, path, kind, options, before, result):
        if not self.connection:
            return
        try:
            if signature(path) != before:
                return  # A live writer changed the file while we read it.
            config = self.config(options)
            data = json.dumps(encode(result), separators=(",", ":"))
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?)",
                                        (self.home, path, kind, config, before, data))
                if kind == "discovery":
                    self.connection.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                                            (self.home, path, config, result.session_key,
                                             result.first_ts.timestamp() if result.first_ts else None,
                                             result.last_ts.timestamp() if result.last_ts else None))
                    self.connection.execute("DELETE FROM identifiers WHERE home=? AND path=? AND config=?", (self.home, path, config))
                    self.connection.executemany("INSERT INTO identifiers VALUES(?,?,?,?,?)",
                        [(self.home, path, config, ident, parent) for parent, ids in
                         ((0, result.links.own_ids), (1, result.links.parent_ids)) for ident in ids])
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.warning = "Workflow cache could not save an entry; results were computed from logs."

    def prune(self, paths):
        if self.connection:
            try:
                retained = set(paths)
                obsolete = {r[0] for r in self.connection.execute("SELECT DISTINCT path FROM entries WHERE home=?", (self.home,))} - retained
                with self.connection:
                    for path in obsolete:
                        for table in ("entries", "sessions", "identifiers"):
                            self.connection.execute(f"DELETE FROM {table} WHERE home=? AND path=?", (self.home, path))
            except sqlite3.Error:
                self.warning = "Workflow cache cleanup deferred."

    def recent_keys(self, options, days):
        """Use indexed timestamps while retaining complete linked families."""
        if not self.connection:
            return None
        try:
            config = self.config(options)
            newest = self.connection.execute("SELECT MAX(last_ts) FROM sessions WHERE home=? AND config=?",
                                             (self.home, config)).fetchone()[0]
            if newest is None:
                return None
            return {r[0] for r in self.connection.execute(
                "SELECT session_key FROM sessions WHERE home=? AND config=? AND last_ts>=?",
                (self.home, config, newest - days * 86400))}
        except (sqlite3.Error, TypeError, ValueError):
            return None

    def identifier_paths(self, identifier, options):
        if not identifier or not self.connection:
            return set()
        try:
            return {row[0] for row in self.connection.execute(
                "SELECT DISTINCT path FROM identifiers WHERE home=? AND config=? AND identifier=? AND is_parent=0",
                (self.home, self.config(options), identifier))}
        except sqlite3.Error:
            return set()

    def stats(self):
        return {"cached": self.hits, "processed": self.misses, "bytes_read": self.bytes_read,
                "seconds": round(time.monotonic() - self.started, 3)}

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
