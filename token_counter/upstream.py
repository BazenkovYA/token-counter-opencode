import asyncio
import time

import httpx

from .accounting import integer, utc_now


def upstream_headers(settings, incoming=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Accept-Encoding": "identity"}
    if settings.auth_mode == "configured_upstream_key":
        headers["Authorization"] = "Bearer " + settings.upstream_key
    elif settings.auth_mode == "passthrough":
        headers["Authorization"] = incoming.get("authorization", "") if incoming else "Bearer " + settings.upstream_key
    # Deliberate allowlist: client metadata, Cookie and arbitrary headers never leave the proxy.
    return headers


class Registry:
    def __init__(self, settings):
        self.settings = settings
        self.detected = {}
        self.detected_at = 0
        self.last_check = 0
        self.lock = asyncio.Lock()
        self.status = {"state": "not_checked", "http_status": None, "checked_at": None}

    def snapshot(self, alias):
        entry = self.settings.models.get(alias, {})
        limit = entry.get("context")
        detected = self.detected.get(alias) if time.monotonic() - self.detected_at < self.settings.health_ttl * 3 else None
        result = {
            "context_limit": limit, "output_limit": entry.get("output"),
            "detected_context_limit": detected,
            "context_limit_source": entry.get("source", "unknown"),
            "context_limit_status": "unverified" if limit else "unknown", "registry_revision": self.settings.revision,
        }
        if detected:
            if limit is None:
                result.update(context_limit=detected, context_limit_source="upstream_models", context_limit_status="verified")
            elif limit == detected:
                result["context_limit_status"] = "verified"
            else:
                result["context_limit_status"] = "conflict"
        return result

    def all(self):
        return [{"alias": alias, "provider_id": self.settings.provider, "integration_profile_id": self.settings.profile,
                 "modalities": entry.get("modalities"), **self.snapshot(alias)} for alias, entry in self.settings.models.items()]

    async def refresh(self, client, force=False):
        if not force and time.monotonic() - self.last_check < self.settings.health_ttl:
            return self.status
        async with self.lock:
            if not force and time.monotonic() - self.last_check < self.settings.health_ttl:
                return self.status
            self.last_check = time.monotonic()
            status = {"state": "unavailable", "http_status": None, "checked_at": utc_now()}
            try:
                async with asyncio.timeout(self.settings.health_timeout):
                    async with client.stream("GET", self.settings.upstream + "/models", headers=upstream_headers(self.settings), timeout=self.settings.health_timeout) as response:
                        status["http_status"] = response.status_code
                        if response.status_code == 200:
                            import json
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 2 * 1024 * 1024:
                                    raise ValueError
                            payload = json.loads(body)
                            if not isinstance(payload.get("data"), list):
                                raise ValueError
                            self.detected = {}
                            for model in payload["data"]:
                                if not isinstance(model, dict):
                                    continue
                                limit = integer(model.get("max_model_len"))
                                if limit and isinstance(model.get("id"), str):
                                    self.detected[model["id"]] = limit
                            self.detected_at = time.monotonic()
                            status["state"] = "ok"
                        elif response.status_code in {401, 403}:
                            status["state"] = "authentication_required"
                        elif response.status_code == 429:
                            status["state"] = "rate_limited"
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
                pass
            self.status = status
            return status

    async def monitor(self, client):
        while True:
            await self.refresh(client)
            await asyncio.sleep(self.settings.health_ttl)
