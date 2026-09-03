import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from conftest import make_settings
from token_counter.config import ConfigurationError, load_settings, normalize_base
from token_counter.upstream import Registry


@pytest.mark.parametrize("base,expected",[("https://example.test/litellm/v1/","https://example.test/litellm/v1"),("http://127.0.0.1:8000","http://127.0.0.1:8000/v1")])
def test_url_prefix(base,expected):assert normalize_base(base)==expected


@pytest.mark.parametrize("base",["http://example.test/v1","https://user:secret@example.test/v1","https://example.test/v1?key=secret"])
def test_bad_urls(base):
    with pytest.raises(ConfigurationError):normalize_base(base)


def test_loopback_and_profile_guard(tmp_path):
    settings=make_settings(tmp_path)
    with pytest.raises(ConfigurationError):replace(settings,upstream="http://localhost:8001/v1").validate()
    with pytest.raises(ConfigurationError):replace(settings,host="0.0.0.0").validate()
    with pytest.raises(ConfigurationError):replace(settings,profile="unsupported").validate()


async def test_detection_conflict_unknown_and_timeout(tmp_path):
    settings=make_settings(tmp_path,health_timeout=.02,health_ttl=.02)
    registry=Registry(settings);model=next(iter(settings.models))
    def handle(request):return httpx.Response(200,json={"data":[{"id":model,"max_model_len":16384},{"id":"new","max_model_len":40000}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await registry.refresh(client)
    assert registry.snapshot(model)["context_limit_status"]=="conflict"
    assert registry.snapshot("new")["context_limit"]==40000
    assert registry.snapshot("absent")["context_limit"] is None
    async def hung(request):await asyncio.sleep(2);return httpx.Response(200,json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(hung)) as client:
        await registry.refresh(client,force=True)
    assert registry.status["state"]=="unavailable"


def test_env_conflicts_and_secret_not_repr(tmp_path,monkeypatch):
    for name in list(__import__('os').environ):
        if name.startswith("TOKEN_COUNTER_"):monkeypatch.delenv(name)
    path=tmp_path/".env"
    data={"INTEGRATION_PROFILE":"opencode_litellm","AUTH_MODE":"configured_upstream_key","UPSTREAM_BASE_URL":"https://example.test/litellm/v1","UPSTREAM_API_KEY":"u"*32,"CLIENT_KEY":"c"*32,"ADMIN_KEY":"a"*32}
    def write():path.write_text("\n".join("TOKEN_COUNTER_"+key+"="+value for key,value in data.items()))
    write();settings=load_settings(path);assert "c"*32 not in repr(settings)
    data["UPSTREAM"]="http://127.0.0.1:8002";write()
    with pytest.raises(ConfigurationError):load_settings(path)
    data.pop("UPSTREAM");data["CONTEXT_LIMIT"]="24576";write()
    with pytest.raises(ConfigurationError):load_settings(path)
