from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PROFILE = "opencode_litellm"


class ConfigurationError(ValueError):
    pass


def normalize_base(value: str) -> str:
    url = urlsplit(value.strip().rstrip("/"))
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ConfigurationError("Upstream должен быть HTTP(S) API base URL")
    if url.username or url.password or url.query or url.fragment:
        raise ConfigurationError("Credentials, query и fragment запрещены в upstream URL")
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("HTTP без TLS допустим только на loopback")
    path = url.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def positive(value, label: str, *, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ConfigurationError(f"{label}: требуется положительное целое число")
    return value


@dataclass(repr=False)
class Settings:
    profile: str
    upstream: str
    provider: str
    data_dir: Path
    log_dir: Path
    client_key: str = field(repr=False)
    admin_key: str = field(repr=False)
    upstream_key: str = field(default="", repr=False)
    auth_mode: str = "none"
    host: str = "127.0.0.1"
    port: int = 8001
    instance_id: str = "local"
    models: dict = field(default_factory=dict)
    revision: str = ""
    metadata_source: str = "none"
    metadata_path: Path | None = None
    include_usage: bool = True
    connect_timeout: float = 10.0
    idle_timeout: float = 180.0
    total_timeout: float = 1800.0
    health_timeout: float = 2.0
    health_ttl: float = 30.0
    max_body_bytes: int = 32 * 1024 * 1024
    queue_size: int = 256
    queue_retries: int = 2
    demo: bool = False

    @property
    def db_path(self):
        return self.data_dir / "usage.db"

    def validate(self):
        if self.profile != PROFILE:
            raise ConfigurationError("Поддерживается только профиль opencode_litellm")
        self.upstream = normalize_base(self.upstream)
        if self.host != "127.0.0.1" or not 1 <= self.port <= 65535:
            raise ConfigurationError("Разрешён bind 127.0.0.1 и порт 1–65535")
        target = urlsplit(self.upstream)
        target_port = target.port or (443 if target.scheme == "https" else 80)
        loopback = target.hostname == "localhost"
        try:
            loopback |= ipaddress.ip_address(target.hostname).is_loopback
        except ValueError:
            pass
        if loopback and target_port == self.port:
            raise ConfigurationError("Upstream указывает на порт самого proxy")
        for key in (self.client_key, self.admin_key):
            if len(key) < 24 or "<" in key or "\n" in key or "\r" in key:
                raise ConfigurationError("Локальные ключи должны содержать минимум 24 символа")
        if self.client_key == self.admin_key:
            raise ConfigurationError("Client и admin keys должны различаться")
        if self.auth_mode not in {"none", "configured_upstream_key", "passthrough"}:
            raise ConfigurationError("Неизвестный auth mode")
        if self.auth_mode == "configured_upstream_key" and (
            not self.upstream_key or "<" in self.upstream_key or "\n" in self.upstream_key
        ):
            raise ConfigurationError("Требуется upstream key (значение не выводится)")
        if self.upstream_key in {self.client_key, self.admin_key}:
            raise ConfigurationError("Upstream key должен отличаться от локальных ключей")
        if self.auth_mode == "passthrough":
            # Only explicit allowlisted credentials are accepted, never arbitrary bearer values.
            if not self.upstream_key:
                raise ConfigurationError("Passthrough требует ожидаемый upstream key для проверки клиента")
        for alias, entry in self.models.items():
            if not isinstance(alias, str) or not alias or len(alias) > 256:
                raise ConfigurationError("Некорректный alias")
            positive(entry.get("context"), "context")
            positive(entry.get("output"), "output")
        if min(self.connect_timeout, self.idle_timeout, self.total_timeout, self.health_timeout, self.health_ttl) <= 0:
            raise ConfigurationError("Timeout/TTL должны быть положительными")
        if not 1 <= self.queue_size <= 10000 or not 0 <= self.queue_retries <= 5:
            raise ConfigurationError("Недопустимые границы очереди")
        return self


def load_settings(env_file: str | Path | None = None) -> Settings:
    env_file = Path(env_file or ROOT / "runtime" / "opencode_litellm" / ".env").resolve()
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}

    def get(name, default=""):
        return values.get("TOKEN_COUNTER_" + name, default) or default

    def path(name, default=""):
        value = get(name, default)
        if not value:
            return None
        result = Path(value).expanduser()
        return (ROOT / result).resolve() if not result.is_absolute() else result.resolve()

    profile = get("INTEGRATION_PROFILE", PROFILE)
    if profile != PROFILE:
        raise ConfigurationError("Эта сборка поддерживает только OpenCode / LiteLLM")
    upstream = normalize_base(get("UPSTREAM_BASE_URL", get("UPSTREAM", "https://gateway.example.invalid/v1")))
    if get("UPSTREAM") and normalize_base(get("UPSTREAM")) != upstream:
        raise ConfigurationError("UPSTREAM и UPSTREAM_BASE_URL расходятся")
    registry_path = path("MODEL_REGISTRY", "config/models.json")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        models = registry["profiles"][profile]["models"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("Не удалось прочитать реестр моделей выбранного профиля") from None
    if get("MODEL") or get("CONTEXT_LIMIT"):
        raise ConfigurationError("Singleton MODEL/CONTEXT_LIMIT нельзя применять к LiteLLM")
    source = get("SESSION_METADATA_SOURCE", "none")
    if source not in {"none", "sqlite_readonly"}:
        raise ConfigurationError("Неизвестный metadata source")
    upstream_key = get("UPSTREAM_API_KEY")
    if reference := re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", upstream_key):
        upstream_key = os.environ.get(reference[1], "")
        if not upstream_key:
            raise ConfigurationError("Переменная окружения с upstream key недоступна процессу счётчика")
    settings = Settings(
        profile=profile, upstream=upstream,
        provider=get("PROVIDER_ID", "bifrost-litellm"),
        data_dir=path("DATA_DIR", f"runtime/{profile}/data"),
        log_dir=path("LOG_DIR", f"runtime/{profile}/logs"),
        client_key=get("CLIENT_KEY"), admin_key=get("ADMIN_KEY"),
        upstream_key=upstream_key, auth_mode=get("AUTH_MODE", "configured_upstream_key"),
        host=get("HOST", "127.0.0.1"), port=int(get("PORT", "8001")),
        instance_id=get("CLIENT_INSTANCE_ID", "local"), models=models,
        revision=hashlib.sha256(json.dumps(models, sort_keys=True).encode()).hexdigest()[:16],
        metadata_source=source,
        metadata_path=path("OPENCODE_DB") if source != "none" else None,
        include_usage=get("INCLUDE_STREAM_USAGE", "true").lower() == "true",
        connect_timeout=float(get("CONNECT_TIMEOUT", "10")), idle_timeout=float(get("IDLE_TIMEOUT", "180")),
        total_timeout=float(get("TOTAL_TIMEOUT", "1800")), health_timeout=float(get("HEALTH_TIMEOUT", "2")),
        health_ttl=float(get("HEALTH_TTL", "30")),
        demo=get("DEMO", "false").lower() == "true",
    )
    return settings.validate()
