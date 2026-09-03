"""Portable, read-only installation audit. It never prints credential values."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from token_counter.cli import status
from token_counter.config import ConfigurationError, load_settings

parser = argparse.ArgumentParser()
parser.add_argument("--env", default=str(ROOT / "runtime/opencode_litellm/.env"))
parser.add_argument("--config", default=str(Path.home() / ".config/opencode/opencode.json"))
args = parser.parse_args()
checks = {"root": str(ROOT), "python": sys.version.split()[0], "root_files": {}, "env": {}, "service": {}, "opencode": {}}
ok = True
for relative in ["README.md", "AGENTS.md", "requirements.lock", "config/models.json", "token_counter/static/index.html"]:
    present = (ROOT / relative).is_file()
    checks["root_files"][relative] = present
    ok &= present
try:
    settings = load_settings(args.env)
    checks["env"] = {"valid": True, "profile": settings.profile, "port": settings.port,
                     "data_inside_root": settings.data_dir.is_relative_to(ROOT),
                     "log_inside_root": settings.log_dir.is_relative_to(ROOT), "models": len(settings.models)}
    checks["service"] = status(args.env)
    checks["service"].get("health", {}).pop("instance_nonce", None)
except (ConfigurationError, OSError, ValueError) as exc:
    checks["env"] = {"valid": False, "error": str(exc)}
    ok = False
try:
    data = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    options = data["provider"]["bifrost-litellm"]["options"]
    expected = "{file:" + (Path(args.env).resolve().parent / "client.key").as_posix() + "}"
    checks["opencode"] = {"config_found": True, "local_base_url": options.get("baseURL") == "http://127.0.0.1:8001/v1",
                          "local_key_reference": options.get("apiKey") == expected,
                          "plugin_found": (Path(args.config).resolve().parent / "plugins/token-counter.js").is_file()}
except (OSError, ValueError, KeyError, TypeError):
    checks["opencode"] = {"config_found": False}
print(json.dumps({"ok": bool(ok), "checks": checks}, ensure_ascii=False, indent=2))
raise SystemExit(0 if ok else 1)
