import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from token_counter.config import ROOT, ConfigurationError
from token_counter.connection import apply, prepare, rollback

if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr,"reconfigure"):sys.stderr.reconfigure(encoding="utf-8")
parser=argparse.ArgumentParser(description="Подготовка/явное подключение OpenCode к счётчику")
parser.add_argument("action",choices=["prepare","apply","rollback"])
parser.add_argument("--config",default=str(Path.home()/".config/opencode/opencode.json"))
parser.add_argument("--env",default=str(ROOT/"runtime/opencode_litellm/.env"))
args=parser.parse_args()
try:
    result=prepare(args.config,args.env) if args.action=="prepare" else apply(args.env) if args.action=="apply" else rollback(args.env)
    print(json.dumps(result,ensure_ascii=False,indent=2))
except Exception as exc:
    print(str(exc) if isinstance(exc,ConfigurationError) else "Операция не выполнена; секретные значения не выводятся",file=sys.stderr)
    raise SystemExit(1) from None
