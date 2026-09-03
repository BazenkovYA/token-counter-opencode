import json
import sqlite3
import time
from dataclasses import replace

import pytest

from conftest import make_settings
from token_counter.accounting import Usage, new_event, utc_now
from token_counter.exporting import export_bundle, verify_bundle
from token_counter.metadata import Metadata
from token_counter.storage import EventSink, Store
from token_counter.upstream import Registry


def add(store, settings, sequence=1, chat="a", kind="main", usage=True):
    event=new_event(settings,{"model":next(iter(settings.models)),"messages":[]},{"x-token-counter-client":"opencode","x-token-counter-session-id":chat,"x-token-counter-request-kind":kind},time.time_ns()+sequence)
    event.update(Registry(settings).snapshot(event["requested_model"]))
    event.update(request_status="completed",http_status=200,finished_at=utc_now(),duration_ms=1)
    u=Usage()
    if usage:u.observe({"usage":{"prompt_tokens":sequence,"completion_tokens":2}})
    u.apply(event);store.insert(event)
    return event


def test_stable_cursor_offset_and_filters(tmp_path):
    settings=make_settings(tmp_path);store=Store(settings.db_path,settings.admin_key);store.initialize()
    for n in range(1,105):add(store,settings,n)
    first=store.history(limit=50)
    add(store,settings,999)
    second=store.history(limit=50,cursor=first["next_cursor"])
    third=store.history(limit=50,cursor=second["next_cursor"])
    ids=[r["id"] for page in (first,second,third) for r in page["items"]]
    assert len(ids)==104 and len(set(ids))==104 and max(ids)==104
    offset=store.history(limit=50,offset=50,snapshot=first["snapshot"])
    assert [r["id"] for r in offset["items"]]==[r["id"] for r in second["items"]]
    with pytest.raises(ValueError):store.history({"session_id":"b"},cursor=first["next_cursor"])
    with pytest.raises(ValueError):store.history(cursor=first["next_cursor"]+"bad")


def test_restart_history_profile_limits_and_last_complete(tmp_path):
    settings=make_settings(tmp_path);store=Store(settings.db_path,settings.admin_key);store.initialize()
    alias=next(iter(settings.models))
    old=replace(settings,models={alias:{"context":16384,"output":131072,"source":"opencode_config"}})
    add(store,old,1);add(store,settings,2);add(store,settings,3,usage=False)
    restarted=Store(settings.db_path,settings.admin_key)
    assert restarted.check()==3
    rows=restarted.history()["items"]
    assert rows[-1]["context_limit"]==16384 and rows[0]["context_limit"]==204800
    current=restarted.stats()["current"][0]
    assert current["stale"] and current["prompt_tokens"] is None and current["last_complete"]["prompt_tokens"]==2


def test_rare_dialog_and_source_namespace(tmp_path):
    settings=make_settings(tmp_path);store=Store(settings.db_path);store.initialize()
    add(store,settings,1,"rare")
    for n in range(2,30):add(store,settings,n,"busy")
    other=replace(settings,instance_id="another-install")
    add(store,other,30,"rare")
    assert len(store.stats()["current"])==3
    with pytest.raises(ValueError):store.latest({"session_id":"rare"})
    assert store.latest({"session_id":"rare","client_instance_id":"local"})["prompt_tokens"]==1


def test_metadata_readonly_rename_deleted_missing_and_unknown_schema(tmp_path):
    table="session"
    path=tmp_path/"external.db"
    con=sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} (id TEXT,title TEXT)");con.execute("CREATE TABLE private_messages (body TEXT)")
    con.execute("INSERT INTO private_messages VALUES ('PRIVATE_TEXT')")
    con.execute(f"INSERT INTO {table} VALUES ('a','Первое название')");con.commit()
    settings=make_settings(tmp_path,metadata_source="sqlite_readonly",metadata_path=path)
    reader=Metadata(settings,ttl=0);reader.refresh()
    assert list(reader.items.values())[0]["title"]=="Первое название"
    con.execute(f"UPDATE {table} SET title='Новое название'");con.commit();reader.refresh()
    assert list(reader.items.values())[0]["title"]=="Новое название"
    con.execute(f"DELETE FROM {table}");con.commit();reader.refresh();assert not reader.items
    con.execute(f"DROP TABLE {table}");con.commit();reader.refresh();assert reader.status["state"]=="unsupported_schema"
    assert con.execute("SELECT body FROM private_messages").fetchone()[0]=="PRIVATE_TEXT"
    con.close();path.unlink();reader.refresh();assert reader.status["state"]=="unavailable"


def test_export_same_snapshot_filtered_backup_checksums_and_injection(tmp_path,monkeypatch):
    settings=make_settings(tmp_path);store=Store(settings.db_path);store.initialize()
    add(store,settings,1,chat="=SUM(1,2)");add(store,settings,2,chat="excluded")
    original=store.backup
    def backup_then_new_event(target):
        original(target);add(store,settings,3,chat="=SUM(1,2)")
    monkeypatch.setattr(store,"backup",backup_then_new_event)
    destination=tmp_path/"export"
    archive=export_bundle(store,destination,{"session_id":"=SUM(1,2)"})
    assert archive.is_file()
    result=verify_bundle(destination);assert result["rows"]==1
    assert store.check()==3
    line=json.loads((destination/"events.jsonl").read_text())
    assert line["session_id"]=="=SUM(1,2)" and "title" not in line
    assert "'=SUM(1,2)" in (destination/"events.csv").read_text(encoding="utf-8-sig")
    (destination/"metadata.json").write_text("tampered")
    with pytest.raises(ValueError):verify_bundle(destination)


async def test_queue_bound_and_locked_database(tmp_path):
    settings=make_settings(tmp_path);store=Store(settings.db_path);store.initialize()
    event=add(store,settings)
    sink=EventSink(store,size=1,retries=0)
    sink.enqueue(event);sink.enqueue(event)
    assert sink.lost==1 and sink.degraded
    sink.queue.get_nowait();sink.queue.task_done()
    con=sqlite3.connect(settings.db_path);con.execute("BEGIN IMMEDIATE")
    event["event_uuid"]="new-event"
    sink.start();sink.enqueue(event);await sink.queue.join()
    assert sink.lost==2 and sink.write_failures==1
    con.rollback();con.close();await sink.close()


def test_existing_schema_is_never_implicitly_migrated(tmp_path):
    path=tmp_path/"old.db";con=sqlite3.connect(path)
    con.execute("CREATE TABLE usage_events (id INTEGER,prompt_tokens INTEGER)");con.execute("INSERT INTO usage_events VALUES(1,42)");con.commit();con.close()
    store=Store(path)
    store.backup(tmp_path/"backup.db")
    with pytest.raises(ValueError):store.initialize()
    with sqlite3.connect(path) as con:assert con.execute("SELECT prompt_tokens FROM usage_events").fetchone()[0]==42
