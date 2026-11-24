import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from app.skills.runner import ScriptError, run_script

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _output_dir() -> tuple[Path, str]:
    rel = f"outputs/test-media-{uuid.uuid4().hex}"
    return ROOT / rel, rel


def _payload(output_dir: str, *, voice: bool = False) -> dict:
    return {
        "inputs": {
            "topic": "AI 周报自动化",
            "output_dir": output_dir,
            "duration_seconds": 8,
            "platform": "douyin",
            "theme": "dark",
            "voice": voice,
        },
        "output": {
            "title": "3 小时周报，20 分钟搞定",
            "narration": "重复的整理交给流程。把时间留给判断。",
            "hashtags": ["效率工具", "#AI"],
            "scenes": [
                {"text": "重复的整理交给流程。", "seconds": 4},
                {"text": "把时间留给判断。", "seconds": 4},
            ],
        },
    }


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg 与 ffprobe")
async def test_short_video_renders_real_mp4():
    target, rel = _output_dir()
    try:
        result = await run_script(
            SKILLS / "short-video" / "scripts" / "render_video.py",
            _payload(rel),
            SKILLS / "short-video",
            timeout=180,
        )
        assert result["kind"] == "video"
        assert result["audio"]["status"] == "disabled"
        assert result["width"] == 1080 and result["height"] == 1920
        assert 7.5 <= result["duration_seconds"] <= 9.5
        assert result["hashtags"] == ["#效率工具", "#AI"]

        files = {item["name"]: Path(item["path"]) for item in result["files"]}
        assert {
            "video.mp4",
            "cover.png",
            "subtitles.srt",
            "script.txt",
        } <= set(files)
        assert all(path.is_file() for path in files.values())
        assert files["video.mp4"] == (target / "video.mp4").resolve()
        assert result["preview"] == "video.mp4"
        assert "00:00:0" in files["subtitles.srt"].read_text(encoding="utf-8")

        from PIL import Image

        with Image.open(files["cover.png"]) as cover:
            assert cover.size == (1080, 1920)
    finally:
        shutil.rmtree(target, ignore_errors=True)


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg 与 ffprobe")
async def test_short_video_rejects_empty_scenes():
    target, rel = _output_dir()
    payload = _payload(rel)
    payload["output"]["scenes"] = []
    try:
        with pytest.raises(ScriptError, match="分镜中没有可用文本"):
            await run_script(
                SKILLS / "short-video" / "scripts" / "render_video.py",
                payload,
                SKILLS / "short-video",
                timeout=60,
            )
    finally:
        shutil.rmtree(target, ignore_errors=True)


async def test_short_video_rejects_path_escape():
    payload = _payload("outputs/../escape")
    with pytest.raises(ScriptError, match="outputs"):
        await run_script(
            SKILLS / "short-video" / "scripts" / "render_video.py",
            payload,
            SKILLS / "short-video",
            timeout=30,
        )


async def test_short_video_rejects_absolute_path_escape():
    payload = _payload("/tmp/nmedia-media-escape")
    with pytest.raises(ScriptError, match="outputs"):
        await run_script(
            SKILLS / "short-video" / "scripts" / "render_video.py",
            payload,
            SKILLS / "short-video",
            timeout=30,
        )


@pytest.mark.skipif(
    not (HAS_FFMPEG and os.environ.get("RUN_MEDIA_TTS")),
    reason="联网配音，设置 RUN_MEDIA_TTS=1 且需 ffmpeg",
)
async def test_short_video_muxes_real_voiceover():
    target, rel = _output_dir()
    try:
        result = await run_script(
            SKILLS / "short-video" / "scripts" / "render_video.py",
            _payload(rel, voice=True),
            SKILLS / "short-video",
            timeout=240,
        )
        assert result["audio"]["status"] == "ok"
        assert result["audio"]["segments"] == 2
        files = {item["name"]: Path(item["path"]) for item in result["files"]}
        assert {"video.mp4", "voiceover.mp3"} <= set(files)
        assert files["voiceover.mp3"].stat().st_size > 0
        assert result["duration_seconds"] > 0
    finally:
        shutil.rmtree(target, ignore_errors=True)


@pytest.mark.skipif(
    not os.environ.get("RUN_MEDIA_TTS"), reason="联网合成，设置 RUN_MEDIA_TTS=1 启用"
)
async def test_tts_voiceover_synthesizes_mp3_and_srt():
    target, rel = _output_dir()
    try:
        result = await run_script(
            SKILLS / "tts-voiceover" / "scripts" / "render_voiceover.py",
            {
                "inputs": {
                    "text": "这是一段用于验收的配音文本，检查字幕和音频是否生成。",
                    "title": "配音验收",
                    "output_dir": rel,
                    "voice_name": "zh-CN-XiaoxiaoNeural",
                    "rate": "-5%",
                },
                "output": {"narration": "这是一段用于验收的配音文本，检查字幕和音频是否生成。"},
            },
            SKILLS / "tts-voiceover",
            timeout=180,
        )
        assert result["kind"] == "audio"
        assert result["duration_seconds"] > 0
        files = {item["name"]: Path(item["path"]) for item in result["files"]}
        assert files["voiceover.mp3"].stat().st_size > 0
        assert "00:00:0" in files["subtitles.srt"].read_text(encoding="utf-8")
        json.dumps(result, ensure_ascii=False)
    finally:
        shutil.rmtree(target, ignore_errors=True)
