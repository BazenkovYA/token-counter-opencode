from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from . import __version__
from .accounting import SSEParser, Usage, identifier, integer, new_event, utc_now
from .metadata import Metadata
from .storage import EventSink, Store, filters_from
from .upstream import Registry, upstream_headers

STATIC = Path(__file__).parent / "static"


def error(message, status=400):
    return JSONResponse({"error": {"message": message, "type": "token_counter_error"}}, status_code=status)


def equal_secret(left, right):
    return isinstance(left, str) and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class ManagedStreamResponse(StreamingResponse):
    def __init__(self, content, *, finalize, upstream, **kwargs):
        super().__init__(content, **kwargs)
        self.finalize_event, self.upstream = finalize, upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except (ClientDisconnect, OSError):
            self.finalize_event("cancelled")
        finally:
            # Also handles disconnect before the generator starts, and send() failures.
            self.finalize_event("cancelled")
            await self.body_iterator.aclose()
            await self.upstream.aclose()


class Security:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        settings = self.settings
        hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}
        if settings.port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        host = request.headers.get("host", "").lower()
        if host not in hosts:
            return await error("Host не разрешён", 400)(scope, receive, send)
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://{h}" for h in hosts}:
            return await error("Origin не разрешён", 403)(scope, receive, send)
        path = scope["path"]
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        admin_ok = equal_secret(bearer, settings.admin_key)
        if path.startswith("/v1"):
            expected = settings.upstream_key if settings.auth_mode == "passthrough" else settings.client_key
            if not equal_secret(bearer, expected):
                return await error("Неверный ключ локального proxy", 401)(scope, receive, send)
        elif path.startswith("/internal/") and not admin_ok:
            return await error("Недостаточно прав", 403)(scope, receive, send)
        scope["tc_admin"] = admin_ok

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
                ])
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, safe_send)


async def body_limited(request, limit):
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > limit:
            raise ValueError("Слишком большое тело запроса")
    return bytes(result)


async def response_limited(response, limit):
    result = bytearray()
    async for chunk in response.aiter_bytes():
        result.extend(chunk)
        if len(result) > limit:
            raise ValueError("Слишком большой непотоковый ответ")
    return bytes(result)


def response_headers(response):
    allowed = {"content-type", "retry-after", "x-request-id", "x-litellm-call-id"}
    return {k: v for k, v in response.headers.items() if k in allowed or k.startswith("x-ratelimit-")}


def create_app(settings, transport=None):
    settings.validate()
    store = Store(settings.db_path, settings.admin_key)
    sink = EventSink(store, settings.queue_size, settings.queue_retries)
    registry, metadata = Registry(settings), Metadata(settings)
    active = {}
    last_start = 0

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.check)  # No implicit migrations on service startup.
        timeout = httpx.Timeout(connect=settings.connect_timeout, read=settings.idle_timeout, write=60, pool=10)
        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False) as client:
            app.state.client = client
            sink.start()
            await registry.refresh(client)
            monitor = asyncio.create_task(registry.monitor(client))
            try:
                yield
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                await sink.close()

    app = FastAPI(title="Token Counter", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Security, settings=settings)
    app.state.settings, app.state.store, app.state.sink = settings, store, sink
    app.state.registry, app.state.metadata, app.state.active = registry, metadata, active
    app.state.shutdown = None
    app.state.instance_nonce = ""

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return error("Некорректные параметры запроса")

    @app.exception_handler(sqlite3.Error)
    async def database_unavailable(request, exc):
        return error("История временно недоступна; проверьте состояние БД", 503)

    @app.get("/")
    async def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/assets/{filename}")
    async def assets(filename: str):
        if filename not in {"app.js", "style.css", "favicon.svg", "brand-logo.png"}:
            return error("Файл не найден", 404)
        return FileResponse(STATIC / filename)

    async def detailed_health():
        await registry.refresh(app.state.client)
        db_state = "ok"
        try:
            await asyncio.wait_for(asyncio.to_thread(store.check), .8)
        except (sqlite3.Error, ValueError, OSError, TimeoutError):
            db_state = "unavailable"
        return {"service": "token-counter", "version": __version__, "profile": settings.profile,
                "provider": settings.provider, "database": db_state, "upstream": registry.status,
                "accounting": sink.status(), "active_requests": len(active), "updated_at": utc_now(),
                "demo": settings.demo,
                "state": "ok" if db_state == "ok" and registry.status["state"] == "ok" and not sink.degraded else "degraded"}

    @app.get("/health")
    async def health(request: Request):
        if request.scope.get("tc_admin"):
            return {**await detailed_health(), "instance_nonce": app.state.instance_nonce}
        return {"service": "token-counter", "version": __version__, "ready": True}

    @app.get("/api/health")
    async def api_health():
        return await detailed_health()

    @app.post("/internal/shutdown")
    async def shutdown():
        if app.state.shutdown is None:
            return error("Сервис запущен без менеджера процессов", 409)
        app.state.shutdown()
        return {"ok": True, "active_requests": len(active)}

    @app.get("/api/models")
    async def models():
        return {"items": registry.all(), "profile": settings.profile, "revision": settings.revision}

    @app.get("/api/stats")
    async def stats(request: Request):
        try:
            filters = filters_from(request.query_params)
            data = await asyncio.to_thread(store.stats, filters)
        except ValueError:
            return error("Некорректные фильтры")
        await asyncio.to_thread(metadata.decorate, data["current"])
        await asyncio.to_thread(metadata.decorate, data["recent"])
        data.update(active=list(active.values()), accounting=sink.status(), metadata=metadata.status, filters=filters,
                    scope="Только обращения через этот proxy; суммы относятся к выбранным фильтрам")
        return data

    @app.get("/api/stats/latest")
    async def latest(request: Request):
        try:
            item = await asyncio.to_thread(store.latest, filters_from(request.query_params))
        except ValueError as exc:
            return error(str(exc))
        if item:
            await asyncio.to_thread(metadata.decorate, [item])
        return {"item": item}

    @app.get("/api/history")
    async def history(request: Request):
        try:
            q = request.query_params
            data = await asyncio.to_thread(store.history, filters_from(q), int(q.get("limit", "50")), q.get("cursor"), int(q.get("offset", "0")), q.get("snapshot"))
        except (ValueError, KeyError, TypeError):
            return error("Некорректная пагинация или фильтры")
        await asyncio.to_thread(metadata.decorate, data["items"])
        return data

    @app.get("/api/sessions")
    async def conversations(request: Request):
        known = await asyncio.to_thread(store.known_conversations)
        missing = await asyncio.to_thread(metadata.missing, known)
        return {"items": list(metadata.items.values()), "untracked": missing, "metadata": metadata.status}

    @app.post("/api/export")
    async def export(request: Request):
        from .exporting import export_bundle
        import uuid
        try:
            options = json.loads(await body_limited(request, 8192))
            filters = filters_from(options.get("filters", {}))
            include_titles = options.get("include_titles") is True
            target = settings.data_dir.parent / "exports" / str(uuid.uuid4())
            result = await asyncio.to_thread(export_bundle, store, target, filters, metadata if include_titles else None)
        except (ValueError, TypeError, AttributeError):
            return error("Некорректный экспорт")
        return FileResponse(result, filename="token-counter-export.zip", media_type="application/zip")

    @app.get("/v1/models")
    async def proxy_models(request: Request):
        try:
            async with asyncio.timeout(settings.health_timeout):
                async with app.state.client.stream("GET", settings.upstream + "/models", headers=upstream_headers(settings, request.headers)) as response:
                    data = await response_limited(response, 2 * 1024 * 1024)
                    from starlette.responses import Response
                    return Response(data, status_code=response.status_code, headers=response_headers(response))
        except (httpx.HTTPError, TimeoutError, ValueError):
            return error("Upstream models временно недоступен", 502)

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal last_start
        last_start = max(time.time_ns(), last_start + 1)
        started_ns, start_clock = last_start, time.monotonic()
        usage = Usage()
        event = None
        finished = False
        upstream = None

        def finalize(status):
            nonlocal finished
            if finished or event is None:
                return
            finished = True
            event["request_status"] = status
            event["finished_at"] = utc_now()
            event["duration_ms"] = round((time.monotonic() - start_clock) * 1000, 2)
            usage.apply(event)
            if event.get("fallback_count", 0) or (event.get("response_model") and event["response_model"] != event["requested_model"]):
                event["context_limit_status"] = "unknown"
                event["flags"].append("unresolved_response_model")
            active.pop(event["event_uuid"], None)
            sink.enqueue(event)

        try:
            try:
                if request.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Сжатые запросы не поддержаны; используйте JSON identity")
                payload = json.loads(await body_limited(request, settings.max_body_bytes))
                if not isinstance(payload, dict):
                    raise ValueError("Требуется JSON object")
                event = new_event(settings, payload, request.headers, started_ns)
                event.update(registry.snapshot(event["requested_model"]))
                if not isinstance(payload.get("messages"), list):
                    raise ValueError("Требуется массив messages")
                if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
                    raise ValueError("Поддержан только n=1")
                if type(payload.get("stream", False)) is not bool:
                    raise ValueError("stream должен быть boolean")
                if event["requested_model"] == "no-default-models":
                    raise ValueError("no-default-models не является моделью для генерации")
                if payload.get("stream") and settings.include_usage:
                    options = payload.get("stream_options") or {}
                    if not isinstance(options, dict):
                        raise ValueError("stream_options должен быть object")
                    payload["stream_options"] = {**options, "include_usage": True}
            except (ValueError, TypeError, AttributeError):
                if event is None:
                    event = new_event(settings, {"model": "__invalid_request__", "messages": []}, {}, started_ns)
                    event.update(registry.snapshot("__invalid_request__"))
                event["http_status"] = 400
                usage.flags.add("invalid_request")
                finalize("error")
                return error("Некорректный JSON/идентификатор/параметры; поддержаны messages, n=1 и JSON identity")

            active[event["event_uuid"]] = {key: event.get(key) for key in (
                "event_uuid", "started_at", "started_ns", "conversation_id", "session_id", "client_source", "client_instance_id",
                "requested_model", "request_kind", "integration_profile_id")}
            headers = upstream_headers(settings, request.headers)
            outbound = app.state.client.build_request("POST", settings.upstream + "/chat/completions", json=payload, headers=headers)
            send_task = asyncio.create_task(app.state.client.send(outbound, stream=True))
            try:
                async with asyncio.timeout(settings.total_timeout):
                    while not send_task.done():
                        done, _ = await asyncio.wait({send_task}, timeout=.2)
                        if not done and await request.is_disconnected():
                            raise asyncio.CancelledError
                    upstream = await send_task
            finally:
                if not send_task.done():
                    send_task.cancel()
                    await asyncio.gather(send_task, return_exceptions=True)
            event["http_status"] = upstream.status_code
            for header, field in (("x-litellm-call-id", "upstream_request_id"), ("x-litellm-model-id", "deployment_id"), ("x-litellm-version", "gateway_version")):
                try:
                    event[field] = identifier(upstream.headers.get(header))
                except ValueError:
                    pass
            for header, field in (("x-litellm-attempted-retries", "retry_count"), ("x-litellm-attempted-fallbacks", "fallback_count")):
                try:
                    event[field] = integer(int(upstream.headers[header]))
                except (KeyError, ValueError):
                    pass
            cache = upstream.headers.get("x-litellm-cache-hit", "").lower()
            if cache in {"true", "false"}:
                event["cache_hit"] = cache == "true"
            streaming = payload.get("stream") and "text/event-stream" in upstream.headers.get("content-type", "").lower() and upstream.status_code < 400
            if streaming:
                parser = SSEParser(usage)

                async def stream():
                    status = "error"
                    try:
                        async with asyncio.timeout(max(.01, settings.total_timeout - (time.monotonic() - start_clock))):
                            async for chunk in upstream.aiter_bytes():
                                parser.feed(chunk)
                                yield chunk
                                if usage.done:
                                    break
                        parser.finish()
                        status = "completed" if usage.done and not usage.error else "error"
                        if not usage.done:
                            usage.flags.add("stream_without_done")
                    except asyncio.CancelledError:
                        status = "cancelled"
                        raise
                    except (httpx.HTTPError, TimeoutError):
                        usage.flags.add("stream_transport_error")
                        # No synthetic text or synthetic usage injected into the model stream.
                    finally:
                        finalize(status)
                        await upstream.aclose()
                return ManagedStreamResponse(stream(), finalize=finalize, upstream=upstream, status_code=upstream.status_code, headers=response_headers(upstream))

            async with asyncio.timeout(max(.01, settings.total_timeout - (time.monotonic() - start_clock))):
                data = await response_limited(upstream, settings.max_body_bytes)
            try:
                usage.observe(json.loads(data))
            except (ValueError, TypeError):
                usage.flags.add("non_json_response")
            finalize("completed" if upstream.status_code < 400 and not usage.error and "non_json_response" not in usage.flags else "error")
            from starlette.responses import Response
            return Response(data, status_code=upstream.status_code, headers=response_headers(upstream))
        except (asyncio.CancelledError, ClientDisconnect):
            if event is None:
                event = new_event(settings, {"model": "__incomplete_request__", "messages": []}, {}, started_ns)
                event.update(registry.snapshot("__incomplete_request__"))
            finalize("cancelled")
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, OverflowError):
            if event is not None:
                event["http_status"] = event.get("http_status") or 502
            usage.flags.add("upstream_transport_error")
            finalize("error")
            return error("Upstream недоступен или передал некорректный ответ; повторной генерации не было", 502)
        finally:
            # Streaming generator owns its response until completion/cancellation.
            if upstream is not None and finished:
                await upstream.aclose()

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def unsupported(path: str):
        return error("Маршрут не поддержан. MVP: /v1/models и /v1/chat/completions; /responses требует отдельного адаптера", 501)

    return app
