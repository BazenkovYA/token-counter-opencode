import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from conftest import auth, events, make_settings, request_body
from token_counter.app import create_app
from token_counter.storage import Store


class SlowStream(httpx.AsyncByteStream):
    def __init__(self):self.closed=False
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        await asyncio.sleep(5)
        yield b'data: [DONE]\n\n'
    async def aclose(self):self.closed=True


@asynccontextmanager
async def running_app(tmp_path, handler):
    sock=socket.socket();sock.bind(("127.0.0.1",0));sock.listen();port=sock.getsockname()[1]
    settings=make_settings(tmp_path,port=port)
    Store(settings.db_path).initialize()
    async def wrapped(request):
        if request.url.path.endswith("models"):return httpx.Response(200,json={"data":[]})
        return await handler(request)
    app=create_app(settings,httpx.MockTransport(wrapped))
    server=uvicorn.Server(uvicorn.Config(app,log_level="critical",access_log=False,proxy_headers=False,timeout_graceful_shutdown=1))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:break
        if task.done():await task
        await asyncio.sleep(.01)
    try:
        yield app,settings,f"http://127.0.0.1:{port}"
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,5)
        sock.close()


async def test_real_socket_sse_is_immediate_and_disconnect_is_accounted(tmp_path):
    slow=SlowStream()
    async def handle(request):return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=slow)
    async with running_app(tmp_path,handle) as (app,settings,url):
        begin=time.monotonic()
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream("POST",url+"/v1/chat/completions",json=request_body(settings,stream=True),headers=auth(settings)) as response:
                first=await anext(response.aiter_bytes())
                assert b"first" in first and time.monotonic()-begin<2
        for _ in range(100):
            if slow.closed and not app.state.active:break
            await asyncio.sleep(.02)
        saved=await events(app)
        assert slow.closed and not app.state.active
        assert len(saved)==1 and saved[0]["request_status"]=="cancelled" and saved[0]["total_tokens"] is None


async def test_disconnect_waiting_for_headers_cancels_upstream(tmp_path):
    cancelled=asyncio.Event()
    async def handle(request):
        try:await asyncio.sleep(5)
        finally:cancelled.set()
        return httpx.Response(200,json={})
    async with running_app(tmp_path,handle) as (app,settings,url):
        async with httpx.AsyncClient(trust_env=False) as client:
            task=asyncio.create_task(client.post(url+"/v1/chat/completions",json=request_body(settings),headers=auth(settings)))
            await asyncio.sleep(.2);task.cancel();await asyncio.gather(task,return_exceptions=True)
        await asyncio.wait_for(cancelled.wait(),2)
        for _ in range(100):
            if not app.state.active:break
            await asyncio.sleep(.01)
        saved=await events(app)
        assert len(saved)==1 and saved[0]["request_status"]=="cancelled"
