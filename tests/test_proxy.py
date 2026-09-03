import asyncio
import json
import sqlite3

import httpx
import pytest

from conftest import auth, events, request_body


async def test_json_all_configured_models_and_no_payload_retention(harness):
    app,client,upstream,settings=harness
    for model in settings.models:
        body=request_body(settings,model=model,tools=[{"type":"function","function":{"name":"test","parameters":{"type":"object"}}}],tool_choice="auto",reasoning_effort="high",max_tokens=123)
        body["messages"][0]["content"]=[{"type":"text","text":"PRIVATE_PAYLOAD_SENTINEL"},{"type":"image_url","image_url":{"url":"data:image/png;base64,synthetic"}}]
        response=await client.post("/v1/chat/completions",json=body,headers={**auth(settings),"X-Token-Counter-Session-Id":"session-a","X-Token-Counter-Request-Kind":"main","Cookie":"private-cookie"})
        assert response.status_code==200
        actual=json.loads(upstream.requests[-1].content)
        assert actual==body
        assert "x-chat-id" not in upstream.requests[-1].headers and "cookie" not in upstream.requests[-1].headers
        assert "authorization" not in upstream.requests[-1].headers
        assert upstream.requests[-1].url.path==("/litellm/v1/chat/completions" if settings.profile=="opencode_litellm" else "/v1/chat/completions")
    saved=await events(app)
    assert len(saved)==len(settings.models)
    for event in saved:
        assert event["prompt_tokens"]==7754 and event["total_tokens"]==11014
        assert event["context_limit"]==settings.models[event["requested_model"]]["context"]
    for path in settings.data_dir.iterdir():
        if path.is_file():assert b"PRIVATE_PAYLOAD_SENTINEL" not in path.read_bytes()


@pytest.mark.parametrize("code",[400,401,403,429,500])
async def test_errors_forwarded_once(harness,code):
    app,client,upstream,settings=harness
    upstream.code=code;upstream.usage=None;upstream.response_headers={"Retry-After":"3"}
    response=await client.post("/v1/chat/completions",json=request_body(settings),headers=auth(settings))
    assert response.status_code==code and response.headers["retry-after"]=="3"
    saved=await events(app)
    assert len(saved)==1 and saved[0]["request_status"]=="error" and saved[0]["prompt_tokens"] is None
    assert len([r for r in upstream.requests if r.method=="POST"])==1


async def test_stream_forwarded_final_usage_after_finish(harness):
    app,client,upstream,settings=harness
    model=next(iter(settings.models))
    finish={"model":model,"choices":[{"index":0,"delta":{"content":"Привет 🔧","reasoning_content":"hidden","tool_calls":[{"index":0,"id":"call","function":{"name":"test","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}
    usage={"model":model,"choices":[],"usage":upstream.usage}
    raw=("data: "+json.dumps(finish,ensure_ascii=False)+"\r\n\r\n"+("data: "+json.dumps(usage)+"\n\n")*2+"data: [DONE]\n\n").encode()
    upstream.stream_data=[raw[i:i+47] for i in range(0,len(raw),47)]
    response=await client.post("/v1/chat/completions",json=request_body(settings,stream=True,stream_options={"custom":True,"include_usage":False}),headers=auth(settings))
    assert response.content==raw
    sent=json.loads(upstream.requests[-1].content)
    assert sent["stream_options"]=={"custom":True,"include_usage":True}
    saved=await events(app)
    assert len(saved)==1 and saved[0]["usage_status"]=="complete" and saved[0]["total_tokens"]==11014
    assert saved[0]["finish_reason"]=="tool_calls"


async def test_truncated_stream_has_no_invented_usage(harness):
    app,client,upstream,settings=harness
    upstream.stream_data=[b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n']
    response=await client.post("/v1/chat/completions",json=request_body(settings,stream=True),headers=auth(settings))
    assert response.status_code==200
    saved=await events(app)
    assert saved[0]["request_status"]=="error" and saved[0]["total_tokens"] is None
    assert "stream_without_done" in saved[0]["flags"]


@pytest.mark.parametrize("extra",[{"n":2},{"n":True},{"stream":"true"},{"model":"no-default-models"},{"messages":{}}])
async def test_invalid_inputs_recorded_without_generation(harness,extra):
    app,client,upstream,settings=harness
    response=await client.post("/v1/chat/completions",json=request_body(settings,**extra),headers=auth(settings))
    assert response.status_code==400
    assert not [r for r in upstream.requests if r.method=="POST"]
    assert len(await events(app))==1


async def test_missing_ids_never_group_and_repeated_request_id_never_dedup(harness):
    app,client,upstream,settings=harness
    for _ in range(2):await client.post("/v1/chat/completions",json=request_body(settings),headers={**auth(settings),"X-Request-Id":"same"})
    saved=await events(app)
    assert len(saved)==2 and saved[0]["event_uuid"]!=saved[1]["event_uuid"]
    assert not app.state.store.stats()["current"]


async def test_session_identity_and_conflict(harness):
    app,client,upstream,settings=harness
    for session in ("a","b"):
        response=await client.post("/v1/chat/completions",json=request_body(settings),headers={**auth(settings),"X-Token-Counter-Session-Id":session,"X-Token-Counter-Request-Kind":"main"})
        assert response.status_code==200
    await events(app)
    assert len(app.state.store.stats()["current"])==2
    response=await client.post("/v1/chat/completions",json=request_body(settings),headers={**auth(settings),"X-Token-Counter-Session-Id":"a","X-Token-Counter-Client":"external"})
    assert response.status_code==400


async def test_aux_compaction_unknown_and_no_old_completion_overwrite(harness):
    app,client,upstream,settings=harness
    async def call(kind):
        return await client.post("/v1/chat/completions",json=request_body(settings),headers={**auth(settings),"X-Token-Counter-Session-Id":"a","X-Token-Counter-Request-Kind":kind})
    await call("main");await call("auxiliary");await events(app)
    assert app.state.store.stats()["current"][0]["request_kind"]=="main"
    await call("compaction");await events(app)
    assert app.state.store.stats()["current"][0]["stale"]
    await call("main");await events(app)
    assert not app.state.store.stats()["current"][0]["stale"]
    upstream.usage=None
    await call("main");await events(app)
    assert app.state.store.stats()["current"][0]["stale"]


async def test_database_write_failure_does_not_damage_response(harness,monkeypatch):
    app,client,upstream,settings=harness
    def fail(event):raise sqlite3.OperationalError("synthetic disk full")
    monkeypatch.setattr(app.state.store,"insert",fail)
    response=await client.post("/v1/chat/completions",json=request_body(settings),headers=auth(settings))
    assert response.status_code==200 and response.json()["usage"]["total_tokens"]==11014
    await app.state.sink.queue.join()
    assert app.state.sink.lost==1 and app.state.sink.degraded


async def test_security_and_api_empty_state(harness):
    app,client,upstream,settings=harness
    response=await client.get("/api/stats")
    assert response.status_code==200 and response.json()["totals"]["total_tokens"] is None
    assert (await client.get("/api/stats/latest?session_id=a")).json()=={"item":None}
    assert (await client.get("/",headers={"Host":"evil.example:8001"})).status_code==400
    assert (await client.get("/api/stats",headers={"Origin":"https://evil.example"})).status_code==403
    assert (await client.post("/v1/responses",json={},headers=auth(settings))).status_code==501
    assert (await client.post("/internal/shutdown",headers=auth(settings))).status_code==403


async def test_fallback_and_rate_limit_are_not_context(harness):
    app,client,upstream,settings=harness
    upstream.response_headers={"x-litellm-attempted-fallbacks":"1","x-ratelimit-remaining-tokens":"999999","x-litellm-cache-hit":"true","x-litellm-key-spend":"PRIVATE_SPEND"}
    response=await client.post("/v1/chat/completions",json=request_body(settings),headers=auth(settings))
    saved=await events(app);event=saved[0]
    assert event["context_limit_status"]=="unknown" and event["remaining_after_response"] is None
    assert event["cache_hit"] and event["total_tokens"]==11014
    assert "x-litellm-key-spend" not in response.headers


async def test_out_of_order_finishing(harness):
    app,client,upstream,settings=harness
    count=0
    async def delayed(request):
        nonlocal count
        count+=1;i=count
        await asyncio.sleep(.12 if i==1 else .005)
        return httpx.Response(200,json={"model":next(iter(settings.models)),"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":i*100,"completion_tokens":5}})
    upstream.custom=delayed
    headers={**auth(settings),"X-Token-Counter-Session-Id":"same","X-Token-Counter-Request-Kind":"main"}
    first=asyncio.create_task(client.post("/v1/chat/completions",json=request_body(settings),headers=headers))
    await asyncio.sleep(.015)
    second=asyncio.create_task(client.post("/v1/chat/completions",json=request_body(settings),headers=headers))
    await asyncio.gather(first,second);await events(app)
    assert app.state.store.stats()["current"][0]["prompt_tokens"]==200
