#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$root"
connect=false
demo=false
dev=false
no_start=false
opencode_config="${HOME}/.config/opencode/opencode.json"
python_cmd="${PYTHON:-python3}"
while (($#)); do
  case "$1" in
    --connect-opencode) connect=true; shift ;;
    --demo) demo=true; shift ;;
    --dev) dev=true; shift ;;
    --no-start) no_start=true; shift ;;
    --opencode-config) opencode_config="${2:?missing path}"; shift 2 ;;
    --python) python_cmd="${2:?missing interpreter}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
"$python_cmd" -c 'import sys; assert sys.version_info >= (3,12), sys.version'
[[ -x .venv/bin/python ]] || "$python_cmd" -m venv .venv
py="$root/.venv/bin/python"
"$py" -m pip install --disable-pip-version-check -r requirements.lock
$dev && "$py" -m pip install --disable-pip-version-check -r requirements-dev.lock
folder="$root/runtime/opencode_litellm"
$demo && folder="$root/runtime/demo"
env_file="$folder/.env"
if $demo; then
  [[ -f "$env_file" ]] || "$py" -m token_counter setup --demo --destination "$env_file"
else
  [[ -f "$folder/connection-plan.json" ]] || "$py" scripts/connect_opencode.py prepare --config "$opencode_config" --env "$env_file"
fi
"$py" -m token_counter check --env "$env_file"
$no_start || "$py" -m token_counter start --env "$env_file"
if $connect; then
  [[ "$demo" == false && "$no_start" == false ]] || { echo '--connect-opencode requires a running non-demo counter' >&2; exit 2; }
  "$py" scripts/connect_opencode.py apply --env "$env_file"
  echo 'Restart OpenCode completely, then send one new short request.'
elif [[ "$demo" == false ]]; then
  echo 'Prepared. Re-run with --connect-opencode to apply the reviewed connection.'
fi
echo "Installed from project root: $root"
