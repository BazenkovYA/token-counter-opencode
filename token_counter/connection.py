"""Explicit prepare/apply/rollback for one existing OpenCode provider. No generation."""
import hashlib
import json
import re
from pathlib import Path

from dotenv import dotenv_values, set_key

from .cli import private_file, setup
from .config import ROOT, ConfigurationError, load_settings, normalize_base

PROVIDER = "bifrost-litellm"
LOCAL_BASES = {"http://127.0.0.1:8001/v1", "http://localhost:8001/v1"}


def protected_write(path, content):
    """Atomic replacement with a restrictive ACL before any secret is written."""
    import tempfile
    import os
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".token-counter-", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        private_file(temporary)
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        options = data["provider"][PROVIDER]["options"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("Ожидался существующий JSON-конфиг с provider bifrost-litellm") from None
    jsonc = path.with_suffix(".jsonc")
    if jsonc.exists():
        try:
            other = json.loads(jsonc.read_text(encoding="utf-8-sig"))
            if set(other) - {"$schema"}:
                raise ValueError
        except ValueError:
            raise ConfigurationError("JSONC содержит дополнительные настройки/комментарии: сначала проверьте effective config вручную") from None
    return path, raw, data, options


def previous_connection(config_path, base, key, plugin_source):
    """Validate a prior clone and return its non-secret route plus reusable client key."""
    match = re.fullmatch(r"\{file:(.+)\}", key or "")
    if base not in LOCAL_BASES or not match:
        raise ConfigurationError("Локальный provider должен ссылаться на client.key предыдущей установки")
    client_file = Path(match[1]).expanduser()
    if not client_file.is_absolute() or client_file.name != "client.key":
        raise ConfigurationError("Ссылка предыдущей установки должна вести на абсолютный client.key")
    client_file = client_file.resolve()
    old_env = client_file.parent / ".env"
    old_plan_file = client_file.parent / "connection-plan.json"
    try:
        client_key = client_file.read_text(encoding="utf-8").strip()
        values = dotenv_values(old_env, interpolate=False)
        old_plan = json.loads(old_plan_file.read_text(encoding="utf-8"))
        plugin = Path(old_plan["plugin"]).resolve()
        old_config = Path(old_plan["config"]).resolve()
        upstream = normalize_base(values["TOKEN_COUNTER_UPSTREAM_BASE_URL"])
        upstream_key = values["TOKEN_COUNTER_UPSTREAM_API_KEY"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("Не удалось проверить файлы подключения предыдущей установки") from None
    expected_plugin = config_path.parent / "plugins" / "token-counter.js"
    source_hash = hashlib.sha256(plugin_source.read_bytes()).hexdigest()
    if (old_config != config_path or plugin != expected_plugin.resolve() or
            old_plan.get("provider") != PROVIDER or old_plan.get("changes", {}).get("baseURL") != base or
            old_plan.get("changes", {}).get("apiKey") != key or old_plan.get("plugin_sha256") != source_hash or
            not plugin.exists() or hashlib.sha256(plugin.read_bytes()).hexdigest() != source_hash):
        raise ConfigurationError("Предыдущее подключение или plugin не совпадают с проверенным планом")
    if (len(client_key) < 24 or values.get("TOKEN_COUNTER_CLIENT_KEY") != client_key):
        raise ConfigurationError("Client key предыдущей установки не прошёл проверку")
    if upstream in LOCAL_BASES or not isinstance(upstream_key, str) or not re.fullmatch(r"\{env:[A-Za-z_][A-Za-z0-9_]*\}", upstream_key):
        raise ConfigurationError("Upstream предыдущей установки нельзя безопасно перенести")
    return upstream, upstream_key, client_key


def prepare(config, env):
    path, raw, data, options = read_config(config)
    base = normalize_base(options.get("baseURL", ""))
    env = Path(env).resolve()
    key = options.get("apiKey")
    if data["provider"][PROVIDER].get("npm") != "@ai-sdk/openai-compatible":
        raise ConfigurationError("Адаптер провайдера изменился; требуется проверка маршрута")
    plugin_source = ROOT / "integrations/opencode/token-counter.js"
    migrating = base in LOCAL_BASES
    previous_client_key = None
    if migrating:
        base, key, previous_client_key = previous_connection(path, base, key, plugin_source)
    elif not isinstance(key, str) or not re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", key):
        raise ConfigurationError("Автоподготовка принимает только ссылку {env:NAME}; секреты из OpenCode не копируются")
    configured = data["provider"][PROVIDER].get("models")
    if not isinstance(configured, dict) or not configured:
        raise ConfigurationError("В provider OpenCode не найдены модели")
    models = {}
    for alias, model in configured.items():
        limit = model.get("limit", {}) if isinstance(model, dict) else {}
        context, output = limit.get("context"), limit.get("output")
        if alias == "no-default-models":
            continue
        if (not isinstance(alias, str) or not alias or type(context) is not int or context <= 0 or
                type(output) is not int or output <= 0):
            raise ConfigurationError("Каждая модель OpenCode должна иметь положительные limit.context и limit.output")
        models[alias] = {"context": context, "output": output, "source": "opencode_config",
                         "modalities": model.get("modalities")}
    if not models:
        raise ConfigurationError("В provider OpenCode не найдены модели с положительными лимитами")
    registry = {"version": 1, "profiles": {"opencode_litellm": {"models": models}}}
    if not env.exists():
        setup("opencode_litellm", destination=env)
    registry_path = env.parent / "models.json"
    protected_write(registry_path, (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    try:
        registry_reference = registry_path.relative_to(ROOT).as_posix()
    except ValueError:
        registry_reference = registry_path.as_posix()
    values = dotenv_values(env, interpolate=False)
    if (values.get("TOKEN_COUNTER_INTEGRATION_PROFILE") != "opencode_litellm" or
        values.get("TOKEN_COUNTER_PORT") != "8001" or
        values.get("TOKEN_COUNTER_CLIENT_INSTANCE_ID") != "opencode-local" or
        values.get("TOKEN_COUNTER_DEMO") != "false"):
        raise ConfigurationError("Автоподключение требует рабочего профиля OpenCode, порта 8001 и instance opencode-local")
    updates = {"UPSTREAM_BASE_URL": base, "UPSTREAM_API_KEY": key,
               "AUTH_MODE": "configured_upstream_key", "MODEL_REGISTRY": registry_reference}
    if previous_client_key:
        updates["CLIENT_KEY"] = previous_client_key
    for name, value in updates.items():
        set_key(str(env), "TOKEN_COUNTER_" + name, value, quote_mode="always")
    private_file(env)
    settings = load_settings(env)
    if settings.models != models or settings.port != 8001 or settings.instance_id != "opencode-local" or settings.demo:
        raise ConfigurationError("Effective env/реестр отличается от проверенного плана")
    client_file = env.parent / "client.key"
    protected_write(client_file, settings.client_key.encode("utf-8"))
    changes = {"baseURL": f"http://127.0.0.1:{settings.port}/v1", "apiKey": "{file:" + client_file.as_posix() + "}"}
    plan = {"config": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(), "provider": PROVIDER,
            "upstream": base, "changes": changes, "plugin": str(path.parent / "plugins/token-counter.js"),
            "plugin_sha256": hashlib.sha256(plugin_source.read_bytes()).hexdigest(),
            "plugin_preexisting": migrating,
            "effect": "Изменение только baseURL/apiKey провайдера и установка локального plugin; модели и MCP сохраняются"}
    plan_path = env.parent / "connection-plan.json"
    plan_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
    private_file(plan_path)
    return {"prepared": True, "applied": False, "migrated": migrating, "client_key_reused": bool(previous_client_key),
            "plan": str(plan_path), "env_file": str(env),
            "upstream_key": "ссылка на существующую переменную; значение не сохранено"}


def apply(env):
    env = Path(env).resolve()
    plan = json.loads((env.parent / "connection-plan.json").read_text(encoding="utf-8"))
    path, raw, data, options = read_config(plan["config"])
    plugin = Path(plan["plugin"])
    backup = env.parent / "opencode-before-connect.json"
    settings = load_settings(env)
    from .cli import local_health
    health = local_health(settings)
    if health.get("service") != "token-counter" or health.get("demo") or health.get("profile") != "opencode_litellm" or health.get("database") != "ok":
        raise ConfigurationError("Рабочий счётчик не подтвердил готовность")
    already_applied = all(options.get(key) == value for key, value in plan["changes"].items())
    if already_applied:
        if (not backup.exists() or not plugin.exists() or
            hashlib.sha256(plugin.read_bytes()).hexdigest() != plan["plugin_sha256"]):
            raise ConfigurationError("Локальный маршрут уже указан, но backup/plugin не совпадают с планом")
        return {"applied": True, "already_applied": True, "config": str(path), "plugin": str(plugin),
                "next": "Подключение уже действует; перезапустите OpenCode после обновления plugin"}
    if hashlib.sha256(raw).hexdigest() != plan["source_sha256"]:
        raise ConfigurationError("Конфиг изменён после подготовки; выполните prepare заново")
    plugin_preexisting = bool(plan.get("plugin_preexisting"))
    if plugin.exists() and (not plugin_preexisting or hashlib.sha256(plugin.read_bytes()).hexdigest() != plan["plugin_sha256"]):
        raise ConfigurationError("Существующий plugin не совпадает с проверенным планом")
    if plugin_preexisting and not plugin.exists():
        raise ConfigurationError("Plugin предыдущего подключения исчез после подготовки")
    if backup.exists():
        raise ConfigurationError("Существует backup прошлого подключения; используйте rollback или проверьте вручную")
    source = ROOT / "integrations/opencode/token-counter.js"
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan["plugin_sha256"]:
        raise ConfigurationError("Plugin изменился после prepare")
    protected_write(backup, raw)
    if not plugin_preexisting:
        plugin.parent.mkdir(parents=True,exist_ok=True)
        plugin.write_bytes(source.read_bytes())
    options.update(plan["changes"])
    try:
        protected_write(path, (json.dumps(data,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
    except OSError:
        if not plugin_preexisting:
            plugin.unlink(missing_ok=True)
        raise
    return {"applied":True,"config":str(path),"plugin":str(plugin),"next":"Перезапустите OpenCode самостоятельно; затем выполните разрешённый короткий тест"}


def rollback(env):
    env = Path(env).resolve()
    plan = json.loads((env.parent / "connection-plan.json").read_text(encoding="utf-8"))
    path, raw, data, options = read_config(plan["config"])
    backup = env.parent / "opencode-before-connect.json"
    old = json.loads(backup.read_text(encoding="utf-8-sig"))["provider"][PROVIDER]["options"]
    for key, value in plan["changes"].items():
        if options.get(key) != value:
            raise ConfigurationError("Настройка подключения изменена пользователем; rollback не будет её затирать")
    plugin = Path(plan["plugin"])
    if plugin.exists() and hashlib.sha256(plugin.read_bytes()).hexdigest() != plan["plugin_sha256"]:
        raise ConfigurationError("Plugin изменён пользователем; rollback требует ручной проверки")
    if plan.get("plugin_preexisting") and not plugin.exists():
        raise ConfigurationError("Plugin предыдущего подключения отсутствует; rollback требует ручной проверки")
    for key in plan["changes"]:
        if key in old:options[key]=old[key]
        else:options.pop(key,None)
    protected_write(path, (json.dumps(data,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
    if not plan.get("plugin_preexisting"):
        plugin.unlink(missing_ok=True)
    return {"rolled_back":True,"note":"Восстановлены только поля подключения; остальные настройки и история сохранены"}
