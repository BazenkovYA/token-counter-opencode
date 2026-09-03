from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .accounting import metrics
from .security import sign, unsign

SCHEMA_VERSION = 1
COLUMNS = {
    "event_uuid": "TEXT NOT NULL UNIQUE", "created_at": "TEXT", "created_epoch": "REAL",
    "started_at": "TEXT", "started_ns": "INTEGER NOT NULL", "finished_at": "TEXT",
    "client_source": "TEXT", "client_instance_id": "TEXT", "conversation_id": "TEXT",
    "chat_id": "TEXT", "session_id": "TEXT", "message_id": "TEXT", "parent_session_id": "TEXT", "project_id": "TEXT",
    "client_request_id": "TEXT", "upstream_request_id": "TEXT", "request_kind": "TEXT", "agent_id": "TEXT",
    "integration_profile_id": "TEXT", "provider_id": "TEXT", "requested_model": "TEXT", "response_model": "TEXT", "deployment_id": "TEXT",
    "endpoint": "TEXT", "is_stream": "INTEGER", "message_count": "INTEGER",
    "prompt_tokens": "INTEGER", "completion_tokens": "INTEGER", "total_tokens": "INTEGER",
    "usage_status": "TEXT", "usage_source": "TEXT", "usage_accuracy": "TEXT", "total_tokens_source": "TEXT",
    "usage_details": "TEXT", "usage_observations": "TEXT", "flags": "TEXT",
    "context_limit": "INTEGER", "output_limit": "INTEGER", "detected_context_limit": "INTEGER",
    "context_limit_source": "TEXT", "context_limit_status": "TEXT", "registry_revision": "TEXT",
    "request_status": "TEXT", "http_status": "INTEGER", "finish_reason": "TEXT", "duration_ms": "REAL",
    "retry_count": "INTEGER", "fallback_count": "INTEGER", "cache_hit": "INTEGER", "gateway_version": "TEXT",
}
JSON_COLUMNS = {"usage_details": {}, "usage_observations": [], "flags": []}
FILTER_FIELDS = {
    "client_source", "client_instance_id", "conversation_id", "session_id", "project_id",
    "integration_profile_id", "provider_id", "requested_model", "request_kind", "request_status", "usage_status",
    "usage_accuracy", "usage_source", "registry_revision", "context_limit_status", "context_limit",
}


def filters_from(params):
    result = {}
    for key, value in params.items():
        if not value or key not in FILTER_FIELDS | {"from", "to"}:
            continue
        if len(str(value)) > 256 or any(ord(c) < 32 for c in str(value)):
            raise ValueError("Некорректный фильтр")
        if key in {"from", "to"}:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result[key] = parsed.timestamp()
        elif key == "context_limit":
            result[key] = int(value)
            if result[key] <= 0:
                raise ValueError("Некорректный лимит")
        else:
            result[key] = str(value)
    if result.get("from", float("-inf")) > result.get("to", float("inf")):
        raise ValueError("Начало диапазона позже конца")
    return result


def where_clause(filters, prefix=""):
    conditions, values = [], []
    for key, value in sorted(filters.items()):
        if key in FILTER_FIELDS:
            conditions.append(f'{prefix}"{key}" = ?')
        elif key in {"from", "to"}:
            conditions.append(f'{prefix}created_epoch {">=" if key == "from" else "<="} ?')
        else:
            raise ValueError("Неизвестный фильтр")
        values.append(value)
    return " AND ".join(conditions) or "1=1", values


def decode(row):
    obj = dict(row)
    for key, default in JSON_COLUMNS.items():
        try:
            obj[key] = json.loads(obj.get(key) or json.dumps(default))
        except ValueError:
            obj[key] = default
    obj["is_stream"] = bool(obj.get("is_stream"))
    if obj.get("cache_hit") is not None:
        obj["cache_hit"] = bool(obj["cache_hit"])
    return metrics(obj)


class Store:
    def __init__(self, path: Path, cursor_key=""):
        self.path = Path(path)
        self.cursor_key = cursor_key

    @contextmanager
    def connect(self, readonly=False):
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.15) if readonly else sqlite3.connect(self.path, timeout=.15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=150")
        try:
            yield con
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return self.check()
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables or version:
                raise ValueError("Обнаружена другая схема. Нужны backup и явная миграция; данные не изменены.")
            con.execute("PRAGMA journal_mode=WAL")
            sql = (Path(__file__).parent / "migrations/001_initial.sql").read_text(encoding="utf-8")
            con.executescript(sql)

    def check(self):
        with self.connect(True) as con:
            if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("Версия БД не поддержана; выполните init/migrate явно")
            present = {r[1] for r in con.execute("PRAGMA table_info(usage_events)")}
            if not set(COLUMNS).issubset(present):
                raise ValueError("Схема БД повреждена или несовместима")
            return con.execute("SELECT count(*) FROM usage_events").fetchone()[0]

    def insert(self, event):
        values = [json.dumps(event.get(key, default), ensure_ascii=False) if key in JSON_COLUMNS else event.get(key)
                  for key in COLUMNS for default in [JSON_COLUMNS.get(key)]]
        sql = f'INSERT INTO usage_events ({",".join(COLUMNS)}) VALUES ({",".join("?" for _ in COLUMNS)})'
        with self.connect() as con:
            con.execute(sql, values)
            con.commit()

    def history(self, filters=None, limit=50, cursor=None, offset=0, snapshot=None):
        filters = filters or {}
        if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000 or (cursor and (offset or snapshot)):
            raise ValueError("Некорректная пагинация")
        clause, params = where_clause(filters)
        digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        token = unsign(cursor or snapshot, self.cursor_key) if cursor or snapshot else None
        if token and (token.get("filters") != digest or token.get("v") != 1):
            raise ValueError("Cursor/snapshot относится к другим фильтрам")
        with self.connect(True) as con:
            con.execute("BEGIN")
            upper = token["upper"] if token else con.execute("SELECT coalesce(max(id),0) FROM usage_events").fetchone()[0]
            before = token.get("before", upper + 1) if cursor else upper + 1
            if type(upper) is not int or type(before) is not int or upper < 0 or before < 0:
                raise ValueError("Некорректный cursor")
            rows = con.execute(f"SELECT * FROM usage_events WHERE {clause} AND id<=? AND id<? ORDER BY id DESC LIMIT ? OFFSET ?",
                               [*params, upper, before, limit + 1, offset]).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        context = {"v": 1, "upper": upper, "filters": digest}
        return {
            "items": [decode(row) for row in rows],
            "next_cursor": sign({**context, "before": rows[-1]["id"]}, self.cursor_key) if more else None,
            "snapshot": sign(context, self.cursor_key), "next_offset": offset + len(rows) if more else None,
        }

    def stats(self, filters=None):
        clause, params = where_clause(filters or {})
        with self.connect(True) as con:
            con.execute("BEGIN")
            totals = dict(con.execute(f"""SELECT count(*) AS requests,
                sum(prompt_tokens) AS prompt_tokens, sum(completion_tokens) AS completion_tokens, sum(total_tokens) AS total_tokens,
                sum(usage_status='complete') AS complete, sum(usage_status='partial') AS partial,
                sum(usage_status='unknown') AS unknown, sum(request_status!='completed') AS failed,
                sum(cache_hit=1) AS confirmed_cache_hits,
                count(prompt_tokens) AS known_prompt_count, count(completion_tokens) AS known_completion_count,
                count(total_tokens) AS known_total_count
                FROM usage_events WHERE {clause}""", params).fetchone())
            for key in ("complete", "partial", "unknown", "failed", "confirmed_cache_hits"):
                totals[key] = totals[key] or 0
            totals["incomplete"] = any(totals[k] < totals["requests"] for k in ("known_prompt_count", "known_completion_count", "known_total_count"))
            rows = con.execute(f"""WITH scoped AS (SELECT * FROM usage_events WHERE {clause} AND conversation_id IS NOT NULL),
                ranked AS (SELECT *, row_number() OVER (
                    PARTITION BY client_source,client_instance_id,conversation_id
                    ORDER BY CASE request_kind WHEN 'main' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                    started_ns DESC,id DESC) AS rank,
                    max(CASE WHEN request_kind='compaction' THEN started_ns ELSE 0 END) OVER (
                    PARTITION BY client_source,client_instance_id,conversation_id) AS last_compaction_ns,
                    max(CASE WHEN request_kind='unknown' THEN started_ns ELSE 0 END) OVER (
                    PARTITION BY client_source,client_instance_id,conversation_id) AS last_unknown_ns
                    FROM scoped)
                SELECT * FROM ranked WHERE rank=1 ORDER BY started_ns DESC,id DESC""", params).fetchall()
            recent = con.execute(f"SELECT * FROM usage_events WHERE {clause} ORDER BY id DESC LIMIT 12", params).fetchall()
            complete = con.execute(f"""WITH ranked AS (
                SELECT *, row_number() OVER (PARTITION BY client_source,client_instance_id,conversation_id
                    ORDER BY started_ns DESC,id DESC) rank
                FROM usage_events WHERE {clause} AND conversation_id IS NOT NULL AND request_kind='main'
                    AND usage_status='complete' AND request_status='completed')
                SELECT * FROM ranked WHERE rank=1""", params).fetchall()
            models = con.execute(f"""SELECT integration_profile_id,provider_id,requested_model, count(*) requests,
                sum(prompt_tokens) prompt_tokens,sum(completion_tokens) completion_tokens,sum(total_tokens) total_tokens
                FROM usage_events WHERE {clause} GROUP BY integration_profile_id,provider_id,requested_model""", params).fetchall()
        current = []
        complete_by_chat = {(r["client_source"],r["client_instance_id"],r["conversation_id"]): decode(r) for r in complete}
        for row in rows:
            event = decode(row)
            event["stale"] = event["request_status"] != "completed" or event["usage_status"] != "complete" or event["last_compaction_ns"] > event["started_ns"] or event["last_unknown_ns"] > event["started_ns"]
            event["last_complete"] = complete_by_chat.get((event["client_source"],event["client_instance_id"],event["conversation_id"]))
            event["context_role"] = "main" if event["request_kind"] == "main" else "unknown"
            current.append(event)
        return {"totals": totals, "current": current, "recent": [decode(r) for r in recent], "by_model": [dict(r) for r in models]}

    def latest(self, filters):
        if not any(filters.get(k) for k in ("conversation_id", "session_id")):
            raise ValueError("Укажите Session ID")
        current = self.stats(filters)["current"]
        if len(current) > 1:
            raise ValueError("ID неоднозначен; укажите client_source и client_instance_id")
        return current[0] if current else None

    def known_conversations(self):
        with self.connect(True) as con:
            return {tuple(row) for row in con.execute("SELECT DISTINCT client_source,client_instance_id,conversation_id FROM usage_events WHERE conversation_id IS NOT NULL")}

    def backup(self, target: Path):
        if target.exists():
            raise ValueError("Файл backup уже существует")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(True) as source:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination, pages=256, sleep=.05)
            finally:
                destination.close()


class EventSink:
    def __init__(self, store, size=256, retries=2):
        self.store = store
        self.queue = asyncio.Queue(maxsize=size)
        self.retries = retries
        self.lost = 0
        self.written = 0
        self.write_failures = 0
        self.degraded = False
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.worker())

    def enqueue(self, event):
        try:
            self.queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            self.lost += 1
            self.degraded = True

    async def worker(self):
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                for attempt in range(self.retries + 1):
                    try:
                        await asyncio.to_thread(self.store.insert, event)
                        self.written += 1
                        break
                    except (sqlite3.Error, OSError, ValueError, OverflowError):
                        self.write_failures += 1
                        self.degraded = True
                        if attempt == self.retries:
                            self.lost += 1
                        else:
                            await asyncio.sleep(.05 * (attempt + 1))
            finally:
                self.queue.task_done()

    async def close(self):
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=10)
            except TimeoutError:
                self.lost += self.queue.qsize()
                self.degraded = True
                self.task.cancel()
            else:
                await self.queue.put(None)
            await asyncio.gather(self.task, return_exceptions=True)

    def status(self):
        return {"state": "degraded" if self.degraded else "ok", "pending": self.queue.qsize(),
                "written": self.written, "lost_events": self.lost, "write_failures": self.write_failures,
                "scope": "since_process_start"}
