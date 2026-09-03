import json

import pytest

from token_counter import connection, cli
from token_counter.config import ROOT, ConfigurationError


@pytest.fixture
def staged(tmp_path, monkeypatch):
    # No real OpenCode files or credentials are accessed by these tests.
    monkeypatch.setattr(connection, "private_file", lambda path: path.chmod(0o600))
    monkeypatch.setattr(cli, "private_file", lambda path: path.chmod(0o600))
    monkeypatch.setenv("TEST_COUNTER_EXISTING_KEY", "synthetic-upstream-only")
    registry = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))["profiles"]["opencode_litellm"]["models"]
    config = tmp_path / "opencode.json"
    data = {"mcp": {"keep": {"enabled": True}}, "provider": {"bifrost-litellm": {
        "npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "https://example.test/litellm/v1", "apiKey": "{env:TEST_COUNTER_EXISTING_KEY}"},
        "models": {alias: {"limit": {"context": value["context"], "output": value["output"]}} for alias, value in registry.items()}}}}
    config.write_text(json.dumps(data), encoding="utf-8")
    env = tmp_path / "counter" / ".env"
    monkeypatch.setattr(cli, "local_health", lambda settings: {"service": "token-counter", "profile": "opencode_litellm", "database": "ok", "demo": False})
    return config, env, data


def test_prepare_apply_rollback_preserve_models_mcp(staged):
    config, env, original = staged
    raw = config.read_bytes()
    result = connection.prepare(config, env)
    assert config.read_bytes() == raw and result["applied"] is False
    assert "synthetic-upstream-only" not in (env.parent / "connection-plan.json").read_text(encoding="utf-8")
    assert (env.parent / "data/usage.db").exists()
    connection.apply(env)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["provider"]["bifrost-litellm"]["options"]["baseURL"] == "http://127.0.0.1:8001/v1"
    assert data["provider"]["bifrost-litellm"]["models"] == original["provider"]["bifrost-litellm"]["models"]
    data["mcp"]["added-after-connect"] = {"enabled": False}
    config.write_text(json.dumps(data), encoding="utf-8")
    connection.rollback(env)
    restored = json.loads(config.read_text(encoding="utf-8"))
    assert restored["provider"] == original["provider"]
    assert "added-after-connect" in restored["mcp"]
    assert not (config.parent / "plugins/token-counter.js").exists()


def test_apply_is_idempotent_after_verified_connection(staged):
    config, env, _ = staged
    connection.prepare(config, env)
    connection.apply(env)
    second = connection.apply(env)
    assert second["already_applied"] is True


def test_changed_config_not_overwritten(staged):
    config, env, _ = staged
    connection.prepare(config, env)
    config.write_text(config.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        connection.apply(env)


def test_existing_environment_reference_is_resolved_without_changing_source(staged, monkeypatch):
    config, env, data = staged
    monkeypatch.setenv("TEST_COUNTER_EXISTING_KEY", "synthetic-from-environment")
    data["provider"]["bifrost-litellm"]["options"]["apiKey"] = "{env:TEST_COUNTER_EXISTING_KEY}"
    config.write_text(json.dumps(data), encoding="utf-8")
    connection.prepare(config, env)
    assert json.loads(config.read_text())["provider"]["bifrost-litellm"]["options"]["apiKey"] == "{env:TEST_COUNTER_EXISTING_KEY}"
    assert "synthetic-from-environment" not in env.read_text(encoding="utf-8")
    assert "{env:TEST_COUNTER_EXISTING_KEY}" in env.read_text(encoding="utf-8")
    assert connection.load_settings(env).upstream_key == "synthetic-from-environment"


def test_runtime_registry_is_derived_from_this_opencode_installation(staged):
    config, env, data = staged
    data["provider"]["bifrost-litellm"]["models"] = {
        "Portable model": {"limit": {"context": 300000, "output": 120000}},
        "no-default-models": {"limit": {"context": 0, "output": 1}},
    }
    config.write_text(json.dumps(data), encoding="utf-8")
    connection.prepare(config, env)
    generated = json.loads((env.parent / "models.json").read_text(encoding="utf-8"))
    models = generated["profiles"]["opencode_litellm"]["models"]
    assert models == {"Portable model": {"context": 300000, "output": 120000,
                                          "source": "opencode_config", "modalities": None}}
    assert connection.load_settings(env).models == models


def test_migration_from_previous_clone_reuses_client_key_and_plugin(staged):
    config, old_env, _ = staged
    connection.prepare(config, old_env)
    connection.apply(old_env)
    old_key = (old_env.parent / "client.key").read_text(encoding="utf-8")
    plugin = config.parent / "plugins/token-counter.js"
    new_env = old_env.parent.parent / "new-clone" / ".env"
    result = connection.prepare(config, new_env)
    assert result["migrated"] is True and result["client_key_reused"] is True
    assert (new_env.parent / "client.key").read_text(encoding="utf-8") == old_key
    assert connection.load_settings(new_env).client_key == old_key
    connection.apply(new_env)
    migrated = json.loads(config.read_text(encoding="utf-8"))
    assert str(new_env.parent.as_posix()) in migrated["provider"]["bifrost-litellm"]["options"]["apiKey"]
    assert plugin.exists()
    connection.rollback(new_env)
    restored = json.loads(config.read_text(encoding="utf-8"))
    assert str(old_env.parent.as_posix()) in restored["provider"]["bifrost-litellm"]["options"]["apiKey"]
    assert plugin.exists()


def test_invalid_limit_refused_before_credentials_copied(staged):
    config, env, data = staged
    next(iter(data["provider"]["bifrost-litellm"]["models"].values()))["limit"]["context"] = 0
    config.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        connection.prepare(config, env)
    assert not env.exists()


def test_plugin_conflict_and_user_changes_not_overwritten(staged):
    config, env, data = staged
    connection.prepare(config, env)
    plugin = config.parent / "plugins/token-counter.js"
    plugin.parent.mkdir()
    plugin.write_text("user-plugin")
    with pytest.raises(ConfigurationError):
        connection.apply(env)
    assert plugin.read_text() == "user-plugin"
    plugin.unlink()
    connection.apply(env)
    plugin.write_text("edited-plugin")
    with pytest.raises(ConfigurationError):
        connection.rollback(env)
    assert plugin.read_text() == "edited-plugin"
