from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

from .accounting import utc_now
from .storage import COLUMNS, Store, decode, where_clause


def csv_safe(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def export_bundle(store: Store, destination: Path, filters=None, metadata=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    backup = destination / "usage.db"
    store.backup(backup)
    clause, params = where_clause(filters or {})
    con = sqlite3.connect(backup)
    try:
        con.row_factory = sqlite3.Row
        # Changes are restricted to the export copy, never the source database.
        con.execute(f"DELETE FROM usage_events WHERE NOT coalesce(({clause}),0)", params)
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
        rows = [decode(row) for row in con.execute("SELECT * FROM usage_events ORDER BY id")]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("Export integrity check failed")
    finally:
        con.close()
    title_info = None
    if metadata:
        metadata.refresh(force=True)
        metadata.decorate(rows)
        title_info = dict(metadata.status)
    columns = ["id", *COLUMNS, "occupied_tokens", "remaining_before_response", "remaining_after_response", "utilization_percent", "overflow_tokens"]
    if metadata:
        columns += ["title", "title_source_status"]
    with (destination / "events.jsonl").open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps({key: row.get(key) for key in columns}, ensure_ascii=False) + "\n")
    with (destination / "events.csv").open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: csv_safe(row.get(key)) for key in columns} for row in rows)
    summary = {
        "exported_at": utc_now(), "rows": len(rows), "filters": filters or {}, "integrity_check": integrity,
        "min_id": min((r["id"] for r in rows), default=None), "max_id": max((r["id"] for r in rows), default=None),
        "from": min((r["created_at"] for r in rows), default=None), "to": max((r["created_at"] for r in rows), default=None),
        "usage_status_counts": {s: sum(r["usage_status"] == s for r in rows) for s in ("complete", "partial", "unknown")},
        "usage_sources": sorted({r["usage_source"] for r in rows}), "titles": title_info,
        "snapshot": "SQLite backup API; all data formats and filtered database derive from the same backup",
        "metadata_note": "External titles are read once after the database snapshot; cross-app atomicity is not claimed" if metadata else None,
    }
    (destination / "metadata.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    schema = {"schema_version": 1, "columns": {"id": "INTEGER PRIMARY KEY", **COLUMNS},
              "null": "Unknown, not zero", "derived": "occupied=P+C; remaining=max(0,L-P-C), null for unknown/conflicting L",
              "details": "Cache and reasoning details are included in reported P/C; never add them twice",
              "csv": "Dangerous spreadsheet prefixes escaped; JSONL preserves original values"}
    (destination / "schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(destination.iterdir()) if path.is_file()}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify_bundle(destination)
    archive = destination / "export.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name in [*manifest, "manifest.json"]:
            output.write(destination / name, name)
    return archive


def verify_bundle(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, checksum in manifest.items():
        target = (directory / name).resolve()
        if target.parent != directory or target.is_symlink() or not target.is_file():
            raise ValueError("Недопустимый путь в manifest")
        if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
            raise ValueError("Контрольная сумма не совпала")
    for required in ("usage.db", "events.csv", "events.jsonl", "metadata.json", "schema.json"):
        if required not in manifest:
            raise ValueError("Неполный manifest")
    with Store(directory / "usage.db").connect(True) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("БД не прошла integrity_check")
        count = con.execute("SELECT count(*) FROM usage_events").fetchone()[0]
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    with (directory / "events.jsonl").open(encoding="utf-8") as source:
        json_count = sum(1 for line in source if json.loads(line) is not None)
    with (directory / "events.csv").open(encoding="utf-8-sig", newline="") as source:
        csv_count = sum(1 for _ in csv.DictReader(source))
    if len({count, metadata["rows"], json_count, csv_count}) != 1:
        raise ValueError("Количество строк не совпало")
    return {"ok": True, "rows": count, "integrity_check": "ok", "checksums": len(manifest)}
