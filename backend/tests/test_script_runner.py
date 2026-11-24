import asyncio
import os
from pathlib import Path

import pytest

from app.skills.runner import ScriptError, run_script


def _write(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


async def test_script_receives_payload(tmp_path):
    script = _write(
        tmp_path,
        "echo.py",
        "import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'ok': p['inputs']['topic']}))",
    )
    result = await run_script(script, {"inputs": {"topic": "周报"}}, tmp_path)
    assert result == {"ok": "周报"}


async def test_script_runs_in_skill_dir(tmp_path):
    script = _write(
        tmp_path,
        "cwd.py",
        "import json,os\nprint(json.dumps({'cwd': os.getcwd()}))",
    )
    result = await run_script(script, {}, tmp_path)
    assert Path(result["cwd"]).resolve() == tmp_path.resolve()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程语义")
async def test_script_timeout_kills_child(tmp_path):
    pid_file = tmp_path / "timeout-pid.txt"
    script = _write(
        tmp_path,
        "slow.py",
        "import os,pathlib,time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(5)\n",
    )
    with pytest.raises(ScriptError, match="脚本超时"):
        await run_script(script, {}, tmp_path, timeout=1)
    assert pid_file.exists(), "子进程未启动"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程语义")
async def test_script_timeout_kills_grandchildren(tmp_path):
    heartbeat = tmp_path / "heartbeat.txt"
    child = _write(
        tmp_path,
        "child.py",
        "import pathlib,time\n"
        "while True:\n"
        "    pathlib.Path('heartbeat.txt').write_text(str(time.time()))\n"
        "    time.sleep(0.05)\n",
    )
    script = _write(
        tmp_path,
        "tree.py",
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(30)\n",
    )
    with pytest.raises(ScriptError, match="脚本超时"):
        await run_script(script, {}, tmp_path, timeout=1)
    assert heartbeat.exists(), "孙进程未启动"
    last = heartbeat.read_text()
    await asyncio.sleep(0.3)
    assert heartbeat.read_text() == last, "超时后孙进程仍在运行"


async def test_script_nonzero_exit_includes_stderr(tmp_path):
    script = _write(
        tmp_path,
        "fail.py",
        "import sys\nsys.stderr.write('boom')\nsys.exit(2)",
    )
    with pytest.raises(ScriptError, match="boom"):
        await run_script(script, {}, tmp_path)


async def test_script_invalid_json_rejected(tmp_path):
    script = _write(tmp_path, "bad.py", "print('not json')")
    with pytest.raises(ScriptError, match="不是合法 JSON"):
        await run_script(script, {}, tmp_path)


async def test_script_non_object_json_rejected(tmp_path):
    script = _write(tmp_path, "list.py", "print('[1, 2]')")
    with pytest.raises(ScriptError, match="必须是 JSON 对象"):
        await run_script(script, {}, tmp_path)


async def test_script_empty_stdout_is_empty_object(tmp_path):
    script = _write(tmp_path, "silent.py", "pass")
    assert await run_script(script, {}, tmp_path) == {}


async def test_script_env_is_whitelisted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMEMORY_API_KEY", "secret-value")
    monkeypatch.setenv("JWT_SECRET", "should-not-leak")
    script = _write(
        tmp_path,
        "env.py",
        "import json,os\nprint(json.dumps({'env': dict(os.environ)}))",
    )
    result = await run_script(script, {}, tmp_path)
    keys = set(result["env"])
    assert "OPENMEMORY_API_KEY" not in keys
    assert "JWT_SECRET" not in keys
    assert {"PATH", "PYTHONIOENCODING", "LANG"} <= keys


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程语义")
async def test_cancellation_reaps_child(tmp_path):
    pid_file = tmp_path / "pid.txt"
    script = _write(
        tmp_path,
        "cancel.py",
        "import os,pathlib,time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    task = asyncio.create_task(run_script(script, {}, tmp_path, timeout=10))
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists(), "子进程未启动"
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
