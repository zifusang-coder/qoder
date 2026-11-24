"""edge-tts 配音与 SRT 字幕工具。

edge-tts 为可选依赖（uv sync --extra media）。缺失时抛出 TTSError，
由调用脚本决定是降级为静音视频还是直接失败。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from media_common import MediaError, probe_duration

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_TEXT_CHARS = 2000

VOICES = {
    "zh-CN-XiaoxiaoNeural": "晓晓 · 温暖女声",
    "zh-CN-XiaoyiNeural": "晓伊 · 活泼女声",
    "zh-CN-YunxiNeural": "云希 · 清朗男声",
    "zh-CN-YunyangNeural": "云扬 · 沉稳男声",
    "zh-CN-YunjianNeural": "云健 · 浑厚男声",
    "zh-HK-HiuMaanNeural": "曉曼 · 粤语女声",
    "zh-TW-HsiaoChenNeural": "曉臻 · 台湾女声",
}

_SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")
_RATE = re.compile(r"^[+-]\d{1,2}%$")


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthesisResult:
    path: Path
    srt: str
    duration: float
    voice: str


def normalize_voice(value: object) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate in VOICES:
            return candidate
    return DEFAULT_VOICE


def normalize_rate(value: object) -> str:
    if isinstance(value, str) and _RATE.match(value.strip()):
        return value.strip()
    return "+0%"


def format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def shift_srt(srt: str, offset_seconds: float) -> str:
    if offset_seconds <= 0:
        return srt

    def replace(match: re.Match[str]) -> str:
        hours, minutes, secs, millis = (int(part) for part in match.groups())
        total = hours * 3600 + minutes * 60 + secs + millis / 1000
        return format_srt_time(total + offset_seconds)

    return _SRT_TIME.sub(replace, srt)


def merge_srt(parts: list[str]) -> str:
    cues: list[str] = []
    for part in parts:
        for block in re.split(r"\n\s*\n", part.strip()):
            lines = block.splitlines()
            if not lines:
                continue
            if lines[0].strip().isdigit():
                lines = lines[1:]
            if len(lines) >= 2 and "-->" in lines[0]:
                cues.append("\n".join(lines))
    if not cues:
        return ""
    numbered = [f"{index}\n{cue}" for index, cue in enumerate(cues, start=1)]
    return "\n\n".join(numbered) + "\n"


def scene_srt(texts: list[str], durations: list[float]) -> str:
    parts: list[str] = []
    elapsed = 0.0
    for index, (text, duration) in enumerate(zip(texts, durations, strict=True)):
        start = elapsed
        end = elapsed + max(duration, 0.2)
        parts.append(
            f"{index + 1}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n"
        )
        elapsed = end
    return merge_srt(parts)


def _load_edge_tts():
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSError(
            "未安装 edge-tts，请运行 make media-setup 安装媒体依赖"
        ) from exc
    return edge_tts


def synthesize(
    text: str,
    output_path: Path,
    *,
    voice: object = DEFAULT_VOICE,
    rate: object = "+0%",
    receive_timeout: float = 60,
) -> SynthesisResult:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        raise TTSError("配音文本不能为空")
    if len(cleaned) > MAX_TEXT_CHARS:
        cleaned = cleaned[:MAX_TEXT_CHARS]
    voice_name = normalize_voice(voice)
    edge_tts = _load_edge_tts()
    communicate = edge_tts.Communicate(
        cleaned,
        voice_name,
        rate=normalize_rate(rate),
        boundary="SentenceBoundary",
        receive_timeout=receive_timeout,
    )
    submaker = edge_tts.SubMaker()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async def stream_to_file() -> None:
        with output_path.open("wb") as handle:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                elif chunk["type"] in {"SentenceBoundary", "WordBoundary"}:
                    submaker.feed(chunk)

    try:
        asyncio.run(stream_to_file())
    except TTSError:
        raise
    except Exception as exc:  # noqa: BLE001 - 只暴露异常类型，避免把上游响应写进错误
        output_path.unlink(missing_ok=True)
        raise TTSError(f"edge-tts 合成失败：{type(exc).__name__}") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise TTSError("edge-tts 未返回音频")
    try:
        duration = probe_duration(output_path)
    except MediaError as exc:
        output_path.unlink(missing_ok=True)
        raise TTSError(str(exc)) from exc
    return SynthesisResult(
        path=output_path,
        srt=submaker.get_srt(),
        duration=duration,
        voice=voice_name,
    )
