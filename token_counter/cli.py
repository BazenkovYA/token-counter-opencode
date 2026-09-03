from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import psutil
import uvicorn
from dotenv import dotenv_values

from .config import ROOT, ConfigurationError, load_settings
from .exporting import export_bundle, verify_bundle
from .metadata import Metadata
from .storage import Store, filters_from


def private_file(path):
    path.chmod(0o600)
    if os.name == "nt":
        account = subprocess.check_output(["whoami"], text=True).strip()
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(F)", "SYSTEM:(F)"], capture_output=True)
        if result.returncode:
            raise ConfigurationError("Не удалось ограничить ACL секретного env-файла")


def setup(profile="opencode_litellm", demo=False, destination=None):
    if profile != "opencode_litellm":
        raise ConfigurationError("Эта сборка поддерживает только OpenCode / LiteLLM")
    folder = (ROOT / "runtime" / ("demo" if demo else profile)).resolve()
    env = Path(destination).resolve() if destination else folder / ".env"
    folder = env.parent
    if env.exists():
        raise ConfigurationError("Env уже существует; перезапись ключей запрещена")
    env.parent.mkdir(parents=True, exist_ok=True)
    def portable_path(value):
        value = Path(value).resolve()
        try:
            return value.relative_to(ROOT).as_posix()
        except ValueError:
            return value.as_posix()
    data = {
        "INTEGRATION_PROFILE": profile, "DEMO": "true" if demo else "false",
        "UPSTREAM_BASE_URL": "http://127.0.0.1:8012/v1" if demo else "https://gateway.example.invalid/v1",
        "PROVIDER_ID": "bifrost-litellm", "HOST": "127.0.0.1", "PORT": "8011" if demo else "8001",
        "AUTH_MODE": "none" if demo else "configured_upstream_key",
        "UPSTREAM_API_KEY": "" if demo else "<EXISTING_GATEWAY_KEY>",
        "CLIENT_KEY": secrets.token_urlsafe(32), "ADMIN_KEY": secrets.token_urlsafe(32),
        "CLIENT_INSTANCE_ID": "opencode-local",
        "MODEL_REGISTRY": "config/models.json",
        "DATA_DIR": portable_path(folder / "data"), "LOG_DIR": portable_path(folder / "logs"),
        "SESSION_METADATA_SOURCE": "sqlite_readonly",
        "OPENCODE_DB": portable_path(folder / "demo-metadata.db") if demo else (Path.home() / ".local/share/opencode/opencode.db").as_posix(),
        "INCLUDE_STREAM_USAGE": "true",
    }
    with env.open("x", encoding="utf-8", newline="\n") as output:
        output.write("# Local credentials: do not commit, export or paste into chat.\n")
        for key, value in data.items():
            output.write(f'TOKEN_COUNTER_{key}="{value}"\n')
    private_file(env)
    Store(folder / "data" / "usage.db").initialize()
    if demo:
        from .demo import seed
        seed(load_settings(env))
    return {"env_file": str(env), "database_initialized": True, "demo": demo, "next": "start --env <file>" if demo else "Заполните существующий upstream key в env; затем start"}


def process_file(settings):
    return settings.data_dir.parent / "process.json"


def owned_process(info, env):
    try:
        process = psutil.Process(int(info["pid"]))
        command = process.cmdline()
        return process if (abs(process.create_time() - info["created"]) < .1 and
                           "token_counter" in command and "serve" in command and str(Path(env).resolve()) in command and
                           Path(process.cwd()).resolve() == ROOT) else None
    except (KeyError, ValueError, psutil.Error):
        return None


def read_process(settings):
    try:
        return json.loads(process_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def local_health(settings):
    with httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(f"http://127.0.0.1:{settings.port}/health", headers={"Authorization": "Bearer " + settings.admin_key})
        response.raise_for_status()
        return response.json()


@contextmanager
def start_lock(settings):
    path = settings.data_dir.parent / "start.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            owner = json.loads(path.read_text())
            process = psutil.Process(owner["pid"])
            live = abs(process.create_time() - owner["created"]) < .1
        except (OSError, ValueError, KeyError, psutil.Error):
            if path.exists() and time.time() - path.stat().st_mtime < 30:
                raise ConfigurationError("Другой запуск создаёт lock; повторите после его завершения") from None
            live = False
        if live:
            raise ConfigurationError("Другой запуск уже выполняется")
        path.unlink()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"pid": os.getpid(), "created": psutil.Process().create_time()}, output)
        yield
    finally:
        path.unlink(missing_ok=True)


def start(env):
    import socket
    settings = load_settings(env)
    Store(settings.db_path).check()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    with start_lock(settings):
        existing = read_process(settings)
        if owned_process(existing, env):
            return {"state": "already_running", "pid": existing["pid"], "url": f"http://127.0.0.1:{settings.port}"}
        with socket.socket() as probe:
            try:
                probe.bind((settings.host, settings.port))
            except OSError:
                raise ConfigurationError("Порт занят; чужой процесс не остановлен") from None
        output_path = settings.log_dir / "launcher.log"
        if output_path.exists() and output_path.stat().st_size > 2 * 1024 * 1024:
            output_path.replace(settings.log_dir / "launcher.previous.log")
        with output_path.open("ab") as output:
            child = subprocess.Popen([sys.executable, "-m", "token_counter", "serve", "--env", str(Path(env).resolve())], cwd=ROOT,
                                     stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                     start_new_session=os.name != "nt")
        for _ in range(60):
            if child.poll() is not None:
                raise ConfigurationError("Сервис не запустился; см. launcher.log (секреты не выводятся)")
            try:
                health = local_health(settings)
                info = read_process(settings)
                process = owned_process(info, env)
                related = process and (process.pid == child.pid or child.pid in [p.pid for p in process.parents()])
                if related and health.get("instance_nonce") == info.get("nonce"):
                    return {"state": "running", "pid": process.pid, "url": f"http://127.0.0.1:{settings.port}", "profile": settings.profile,
                            "database": str(settings.db_path), "log": str(settings.log_dir / "service.log"), "demo": settings.demo}
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.25)
        raise ConfigurationError("Запуск не подтвердил готовность; проверьте status и launcher.log")


def stop(env):
    settings = load_settings(env)
    info = read_process(settings)
    process = owned_process(info, env)
    if process is None:
        return {"state": "not_running_or_not_owned", "action": "Ни один процесс не остановлен"}
    health = local_health(settings)
    if health.get("instance_nonce") != info.get("nonce"):
        raise ConfigurationError("HTTP listener не принадлежит PID счётчика; остановка отменена")
    if os.name == "nt":
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.post(f"http://127.0.0.1:{settings.port}/internal/shutdown", headers={"Authorization": "Bearer " + settings.admin_key})
            response.raise_for_status()
    else:
        import signal
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except psutil.TimeoutExpired:
        return {"state": "draining", "pid": process.pid, "message": "Запрошено штатное завершение; принудительного kill не было"}
    return {"state": "stopped", "message": "Остановлен только счётчик; клиенты через proxy временно не могут генерировать"}


def status(env):
    settings = load_settings(env)
    info = read_process(settings)
    result = {"state": "running" if owned_process(info, env) else "not_running", "profile": settings.profile,
              "database": str(settings.db_path), "log": str(settings.log_dir / "service.log")}
    try:
        result["health"] = local_health(settings)
    except (httpx.HTTPError, ValueError):
        result["http"] = "unavailable"
    return result


def serve(env):
    from .app import create_app
    settings = load_settings(env)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(settings.log_dir / "service.log", maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("token_counter")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    transport = None
    if settings.demo:
        from .demo import mock_transport
        transport = mock_transport()
    app = create_app(settings, transport=transport)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning", access_log=False,
                            proxy_headers=False, timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    nonce = secrets.token_hex(16)
    app.state.instance_nonce = nonce
    pid_path = process_file(settings)
    info = {"pid": os.getpid(), "created": psutil.Process().create_time(), "nonce": nonce, "env": str(Path(env).resolve())}
    temporary = pid_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    temporary.replace(pid_path)
    logger.info("service_start profile=%s demo=%s", settings.profile, settings.demo)
    try:
        server.run()
    finally:
        logger.info("service_stop written=%s lost=%s", app.state.sink.written, app.state.sink.lost)
        if read_process(settings).get("nonce") == nonce:
            pid_path.unlink(missing_ok=True)
        handler.close()


def main():
    parser = argparse.ArgumentParser(description="Локальный счётчик токенов")
    sub = parser.add_subparsers(dest="command", required=True)
    setup_cmd = sub.add_parser("setup")
    setup_cmd.add_argument("--profile", choices=["opencode_litellm"], default="opencode_litellm", help=argparse.SUPPRESS)
    setup_cmd.add_argument("--demo", action="store_true")
    setup_cmd.add_argument("--destination")
    for command in ("start", "stop", "status", "serve", "check", "init", "backup", "export"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--env", default=str(ROOT / "runtime/opencode_litellm/.env"))
        if command in {"backup", "export"}:
            cmd.add_argument("--output", required=True)
        if command == "export":
            cmd.add_argument("--filter", action="append", default=[])
            cmd.add_argument("--include-titles", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("directory")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            result = setup(args.profile, args.demo, args.destination)
        elif args.command == "verify":
            result = verify_bundle(args.directory)
        elif args.command in {"start", "stop", "status", "serve"}:
            result = globals()[args.command](args.env)
        else:
            settings = load_settings(args.env)
            store = Store(settings.db_path, settings.admin_key)
            if args.command == "init":
                store.initialize()
                result = {"schema": 1, "database": str(store.path)}
            elif args.command == "check":
                result = {"profile": settings.profile, "rows": store.check(), "models": len(settings.models), "demo": settings.demo}
            elif args.command == "backup":
                store.backup(Path(args.output))
                result = {"backup": str(Path(args.output).resolve())}
            elif args.command == "export":
                filters = filters_from(dict(item.split("=", 1) for item in args.filter))
                result = {"archive": str(export_bundle(store, Path(args.output), filters, Metadata(settings) if args.include_titles else None))}
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ConfigurationError, OSError, ValueError, sqlite3.Error, httpx.HTTPError) as exc:
        # Do not print unexpected exception contents, URL credentials or config values.
        message = str(exc) if isinstance(exc, ConfigurationError) else "Операция не выполнена. Проверьте конфигурацию, пути и доступность сервиса."
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
