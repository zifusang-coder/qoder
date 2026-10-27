import asyncio
import json
import os
import sys
from pathlib import Path


class ScriptError(RuntimeError):
    pass


async def run_script(
    script: Path, payload: dict, cwd: Path, timeout: float = 30.0
) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode()),
            timeout=timeout,
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ScriptError(f"脚本超时（>{timeout}s）") from exc
    if proc.returncode != 0:
        raise ScriptError(stderr.decode(errors="ignore")[-1000:] or "脚本执行失败")
    try:
        return json.loads(stdout.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise ScriptError("脚本输出不是合法 JSON") from exc
