from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .accounting import utc_now


class Metadata:
    """Reads only allowlisted metadata columns. Never reads message payloads."""

    def __init__(self, settings, ttl=10):
        self.settings = settings
        self.ttl = ttl
        self.refreshed = 0
        self.lock = threading.Lock()
        self.items = {}
        self.status = {"state": "disabled", "truncated": False}

    def refresh(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.refreshed < self.ttl:
                return
            self.refreshed = time.monotonic()
            self.items = {}
            path = self.settings.metadata_path
            if self.settings.metadata_source == "none" or path is None:
                self.status = {"state": "disabled", "truncated": False}
                return
            try:
                con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
                try:
                    con.row_factory = sqlite3.Row
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    table, source = "session", "opencode"
                    allowed = ["id", "title", "project_id", "parent_id", "time_created", "time_updated", "time_compacting"]
                    if table not in tables:
                        raise ValueError("unsupported_schema")
                    columns = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                    if not {"id", "title"}.issubset(columns):
                        raise ValueError("unsupported_schema")
                    selected = [c for c in allowed if c in columns]
                    rows = con.execute(f'SELECT {",".join(chr(34)+c+chr(34) for c in selected)} FROM "{table}" ORDER BY id LIMIT 10001').fetchall()
                    for row in rows[:10000]:
                        item = dict(row)
                        if not isinstance(item["id"], str):
                            continue
                        item.update(client_source=source, client_instance_id=self.settings.instance_id, conversation_id=item["id"])
                        item["title"] = str(item.get("title") or "Без названия")[:512]
                        self.items[(source, self.settings.instance_id, item["id"])] = item
                    self.status = {"state": "ok", "truncated": len(rows) > 10000, "updated_at": utc_now(), "count": len(self.items)}
                finally:
                    con.close()
            except (OSError, sqlite3.Error):
                self.status = {"state": "unavailable", "truncated": False}
            except ValueError:
                self.status = {"state": "unsupported_schema", "truncated": False}

    def decorate(self, events):
        self.refresh()
        for event in events:
            key = (event.get("client_source"), event.get("client_instance_id"), event.get("conversation_id"))
            meta = self.items.get(key)
            event["title"] = meta["title"] if meta else "Без названия" if key[2] else "Обращение без ID"
            event["title_source_status"] = "ok" if meta else self.status["state"] if self.status["state"] != "ok" else "not_found"
            if meta and isinstance(meta.get("time_compacting"), (int, float)):
                stamp = meta["time_compacting"]
                compact_ns = stamp * (1_000_000 if stamp > 1e12 else 1_000_000_000)
                if compact_ns > event.get("started_ns", 0):
                    event["stale"] = True
        return events

    def missing(self, known):
        self.refresh()
        return [{**item, "usage_note": "Точный usage отсутствует"} for key, item in self.items.items() if key not in known]
