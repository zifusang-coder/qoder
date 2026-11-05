"""媒体脚本共用的 ffmpeg 封装。

所有命令都以参数列表执行，不经过 shell；调用方只能传入服务端生成的
outputs 目录内路径，因此不存在命令注入面。脚本由 app.skills.runner 以
独立子进程运行，环境变量已被白名单裁剪。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"未找到 {name}，请先安装 ffmpeg（含 ffprobe）")
    return path


def _run(cmd: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffmpeg 处理超时（>{timeout:.0f}s）") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ffmpeg 执行失败").strip()
        raise MediaError(detail[-800:])
    return proc.stdout


def probe_duration(path: Path, *, timeout: float = 30) -> float:
    ffprobe = require_binary("ffprobe")
    output = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout,
    ).strip()
    try:
        duration = float(output)
    except ValueError:
        raise MediaError("ffprobe 未返回有效时长") from None
    if duration <= 0:
        raise MediaError("媒体时长必须大于 0")
    return duration


def _concat_line(path: Path) -> str:
    # ffmpeg concat 语法用单引号包裹路径，内部单引号需写成 '\''
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"


def _write_concat(paths: list[Path], target: Path) -> None:
    target.write_text(
        "\n".join(_concat_line(path) for path in paths) + "\n",
        encoding="utf-8",
    )


def concat_audio(paths: list[Path], output: Path, *, timeout: float = 180) -> None:
    if not paths:
        raise MediaError("没有可拼接的音频")
    ffmpeg = require_binary("ffmpeg")
    listing = output.with_name(f"{output.stem}.concat.txt")
    _write_concat(paths, listing)
    try:
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(output),
            ],
            timeout,
        )
    finally:
        listing.unlink(missing_ok=True)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaError("音频拼接未生成有效文件")


def mux_audio(
    video: Path, audio: Path, output: Path, *, timeout: float = 180
) -> None:
    ffmpeg = require_binary("ffmpeg")
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaError("音视频合成未生成有效文件")


def render_slideshow(
    images: list[Path],
    durations: list[float],
    output: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    timeout: float = 300,
) -> None:
    if not images or len(images) != len(durations):
        raise MediaError("图片与时长数量必须一致")
    if any(duration <= 0 for duration in durations):
        raise MediaError("每个分镜时长必须大于 0")
    ffmpeg = require_binary("ffmpeg")
    cmd: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for image, duration in zip(images, durations, strict=True):
        cmd += [
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-t",
            f"{duration:.3f}",
            "-i",
            str(image),
        ]
    total = sum(durations)
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )
    chains = [f"[{index}:v]{scale}[v{index}]" for index in range(len(images))]
    joined = "".join(f"[v{index}]" for index in range(len(images)))
    filter_complex = ";".join(chains) + (
        f";{joined}concat=n={len(images)}:v=1:a=0[out]"
    )
    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-r",
        str(fps),
        "-t",
        f"{total:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        "-an",
        str(output),
    ]
    _run(cmd, timeout)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaError("视频渲染未生成有效文件")
