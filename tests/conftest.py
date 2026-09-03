import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from token_counter.app import create_app
from token_counter.config import ROOT, Settings
from token_counter.demo import DemoStream
from token_counter.storage import Store


def make_settings(tmp_path, profile="opencode_litellm", **kwargs):
    registry = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
    return Settings(profile=profile, upstream="http://127.0.0.1:8999/litellm/v1",
                    provider="bifrost-litellm",
                    data_dir=tmp_path / "data", log_dir=tmp_path / "logs", client_key="client-test-key-"*3,
                    admin_key="admin-test-key-"*3,
                    models=registry["profiles"][profile]["models"], revision="test-revision", **kwargs).validate()


class Upstream:
    def __init__(self, settings):
        self.settings = settings
        self.requests = []
        self.code = 200
        self.usage = {"prompt_tokens": 7754, "completion_tokens": 3260, "total_tokens": 11014}
        self.response_headers = {}
        self.stream_data = None
        self.models = [{"id": alias, "max_model_len": entry["context"]} for alias, entry in settings.models.items()]
        self.response_model = None
        self.custom = None

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": self.models})
        if self.custom:
            return await self.custom(request)
        body = json.loads(request.content)
        if self.stream_data is not None:
            return httpx.Response(self.code, headers={"content-type":"text/event-stream", **self.response_headers}, stream=DemoStream(self.stream_data))
        content = {"id":"upstream-id", "model":self.response_model or body["model"],
                   "choices":[{"index":0,"message":{"role":"assistant","content":"Синтетический ответ"},"finish_reason":"stop"}]}
        if self.usage is not None:
            content["usage"] = self.usage
        return httpx.Response(self.code, json=content, headers=self.response_headers)


@pytest.fixture
async def harness(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.db_path, settings.admin_key)
    store.initialize()
    upstream = Upstream(settings)
    app = create_app(settings, httpx.MockTransport(upstream.handle))
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001") as client:
            yield app, client, upstream, settings


def auth(settings):
    return {"Authorization": "Bearer " + settings.client_key}


def request_body(settings, **kwargs):
    return {"model":next(iter(settings.models)),"messages":[{"role":"user","content":"synthetic input"}],**kwargs}


async def events(app):
    await app.state.sink.queue.join()
    return app.state.store.history(limit=200)["items"]
