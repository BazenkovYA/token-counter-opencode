import json
import os
import socket
import subprocess
import sys

from token_counter.config import ROOT


def test_native_start_status_duplicate_stop_and_restart(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    env=tmp_path/"test.env"
    values={"INTEGRATION_PROFILE":"opencode_litellm","DEMO":"true","UPSTREAM_BASE_URL":"http://127.0.0.1:8012/v1","PORT":str(port),"AUTH_MODE":"none","CLIENT_KEY":"c"*32,"ADMIN_KEY":"a"*32,"DATA_DIR":(tmp_path/"data").as_posix(),"LOG_DIR":(tmp_path/"logs").as_posix(),"SESSION_METADATA_SOURCE":"none"}
    env.write_text("\n".join("TOKEN_COUNTER_"+k+"="+v for k,v in values.items()),encoding="utf-8")
    def cli(action):
        process=subprocess.run([sys.executable,"-m","token_counter",action,"--env",str(env)],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=30)
        assert process.returncode==0,process.stderr
        return json.loads(process.stdout)
    cli("init")
    try:
        first=cli("start");assert first["state"]=="running"
        duplicate=cli("start");assert duplicate["state"]=="already_running" and duplicate["pid"]==first["pid"]
        status=cli("status");assert status["health"]["database"]=="ok" and status["health"]["demo"]
        assert cli("stop")["state"]=="stopped"
        assert cli("status")["state"]=="not_running"
        assert cli("start")["state"]=="running"
        assert cli("check")["rows"]==0
    finally:
        cli("stop")
