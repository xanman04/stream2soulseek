"""Bounded local cache of public search candidates, never settings or account credentials."""
from __future__ import annotations

from dataclasses import asdict, fields
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ..models import RemoteFile

TTL_S = 12 * 3600
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 256


class DiskCache:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = path, clock

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=.2)
        try:
            conn.execute("PRAGMA auto_vacuum=FULL")
            conn.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, at REAL, complete INTEGER, files TEXT)")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _key(query):
        # v2 invalidates old completeness estimates that counted visible files/peers only.
        return hashlib.sha256(("v2\n" + query).encode("utf-8")).hexdigest()

    def get(self, query):
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT at, complete, files FROM responses WHERE key=?", (self._key(query),)).fetchone()
            if row is None or not 0 <= self.clock() - row[0] < TTL_S or len(row[2]) > MAX_ENTRY_BYTES:
                return None
            raw = json.loads(row[2])
            if not isinstance(raw, list) or not raw or len(raw) > 20000:
                return None
            allowed = {f.name for f in fields(RemoteFile)}
            files = []
            for item in raw:
                if (not isinstance(item, dict) or set(item) - allowed or
                    not isinstance(item.get("username"), str) or not item["username"] or
                    not isinstance(item.get("filename"), str) or not item["filename"] or
                    type(item.get("size")) is not int or item["size"] < 0):
                    return None
                for name in ("bitrate", "sample_rate", "bit_depth", "length_s", "queue_length", "upload_speed"):
                    if item.get(name) is not None and (type(item[name]) is not int or item[name] < 0):
                        return None
                for name in ("is_vbr", "has_free_slot"):
                    if item.get(name) is not None and type(item[name]) is not bool:
                        return None
                if item.get("extension") is not None and not isinstance(item["extension"], str):
                    return None
                files.append(RemoteFile(**item))
            return files, bool(row[1]), self.clock() - row[0]
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return None  # corrupt/unavailable cache is a miss, never a failed search

    def put(self, query, files, complete):
        if not files:
            return  # don't preserve negative results for twelve hours
        try:
            payload = json.dumps([asdict(f) for f in files], ensure_ascii=True)
            if len(payload) > MAX_ENTRY_BYTES:
                return
            now = self.clock()
            with self._connect() as conn:
                conn.execute("DELETE FROM responses WHERE at <= ? OR at > ?", (now - TTL_S, now))
                conn.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)",
                             (self._key(query), now, int(complete), payload))
                rows = conn.execute("SELECT key, length(files) FROM responses ORDER BY at DESC, key").fetchall()
                size = 0
                for index, (key, length) in enumerate(rows):
                    size += length
                    if index >= MAX_ENTRIES or size > MAX_BYTES:
                        conn.execute("DELETE FROM responses WHERE key=?", (key,))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
