"""Synthetic fixtures and a network-free upstream for the isolated demo."""
import json
import time

import httpx

from .accounting import Usage, new_event, utc_now
from .storage import Store
from .upstream import Registry


class DemoStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        import asyncio
        for chunk in self.chunks:
            await asyncio.sleep(.03)
            yield chunk


def mock_transport():
    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GLM-4.7 (res)", "object": "model"}]})
        body = json.loads(request.content)
        model = body.get("model", "demo")
        response = {"id": "demo-response", "object": "chat.completion", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Демонстрационный ответ. Реальная модель не вызывалась."}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7754, "completion_tokens": 3260, "total_tokens": 11014}}
        if body.get("stream"):
            chunks = [f'data: {json.dumps({"id":"demo-response","model":model,"choices":[{"index":0,"delta":{"content":text},"finish_reason":None}]}, ensure_ascii=False)}\n\n'.encode()
                      for text in ["Демонстрационный ", "ответ. ", "Модель не вызывалась."]]
            chunks += [b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                       ("data: " + json.dumps({"model": model, "choices": [], "usage": response["usage"]}) + "\n\n").encode(), b"data: [DONE]\n\n"]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=DemoStream(chunks))
        return httpx.Response(200, json=response)
    return httpx.MockTransport(respond)


def seed(settings):
    from datetime import datetime, timezone
    import sqlite3
    store, registry = Store(settings.db_path), Registry(settings)
    if store.check():
        return
    fixtures = [
        ("demo-api", "GLM-4.7 (res)", 42310, 6820, "main", "completed"),
        ("demo-ui", "Kimi K2.7", 86420, 7320, "main", "completed"),
        ("demo-refactor", "GLM-5.2 (res)", 158200, 11940, "main", "completed"),
        ("demo-tests", "MiniMax-M2.7", None, None, "main", "error"),
        ("demo-api", "GLM-4.7 (res)", 52480, 7910, "main", "completed"),
        ("demo-ui", "Kimi K2.7", 109300, 8900, "compaction", "completed"),
        ("demo-api", "GLM-4.7 (res)", 61580, 10140, "main", "completed"),
    ]
    for i, (session, model, p, c, kind, status) in enumerate(fixtures):
        start = time.time_ns() - (len(fixtures) - i) * 180 * 1_000_000_000
        headers = {"x-token-counter-client": "opencode", "x-token-counter-session-id": session, "x-token-counter-request-kind": kind}
        event = new_event(settings, {"model": model, "messages": [], "stream": True}, headers, start)
        event.update(registry.snapshot(model))
        event.update(request_status=status, http_status=200 if p else 502, finished_at=datetime.fromtimestamp(start/1e9+20, timezone.utc).isoformat(),
                     started_at=datetime.fromtimestamp(start/1e9, timezone.utc).isoformat(), created_at=datetime.fromtimestamp(start/1e9, timezone.utc).isoformat(), duration_ms=20000)
        usage = Usage()
        if p:
            usage.observe({"model": model, "choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p+c}})
        usage.apply(event)
        store.insert(event)
    if settings.metadata_path:
        settings.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(settings.metadata_path)
        try:
            table = "session"
            con.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, title TEXT)")
            con.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", [
                ("demo-api", "Рефакторинг API авторизации"), ("demo-ui", "Новый интерфейс кабинета"),
                ("demo-refactor", "Архитектура сервиса уведомлений"), ("demo-tests", "Тесты обработки ошибок"),
                ("demo-untracked", "Исследование перед подключением счётчика"),
            ])
            con.commit()
        finally:
            con.close()
