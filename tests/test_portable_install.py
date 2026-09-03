import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

from token_counter.config import ROOT


def test_setup_from_different_root_with_spaces_uses_relative_internal_paths(tmp_path):
    clone = tmp_path / "arbitrary clone with spaces"
    shutil.copytree(ROOT / "token_counter", clone / "token_counter")
    shutil.copytree(ROOT / "config", clone / "config")
    env_file = clone / "runtime/demo/.env"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "token_counter", "setup", "--profile", "opencode_litellm",
         "--demo", "--destination", str(env_file)],
        cwd=clone, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    values = dotenv_values(env_file, interpolate=False)
    assert values["TOKEN_COUNTER_MODEL_REGISTRY"] == "config/models.json"
    assert values["TOKEN_COUNTER_DATA_DIR"] == "runtime/demo/data"
    assert values["TOKEN_COUNTER_LOG_DIR"] == "runtime/demo/logs"
    check = subprocess.run([sys.executable, "-m", "token_counter", "check", "--env", str(env_file)],
                           cwd=clone, env=environment, capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stderr
