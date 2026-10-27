import shutil
import uuid
from pathlib import Path

import pytest

from app.skills.runner import ScriptError, run_script

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"


async def test_cover_card_writes_under_outputs():
    rel = f"outputs/test-{uuid.uuid4().hex}"
    target = ROOT / rel
    try:
        result = await run_script(
            SKILLS / "cover-card" / "scripts" / "render_cover.py",
            {
                "inputs": {
                    "title": "AI 周报自动化",
                    "output_dir": rel,
                    "theme": "dark",
                },
                "output": {"subtitle": "把 3 小时压到 20 分钟"},
            },
            SKILLS / "cover-card",
        )
        path = Path(result["path"])
        assert path == (target / "cover.png").resolve()
        assert path.is_file()
        assert result["theme"] == "dark"
    finally:
        shutil.rmtree(target, ignore_errors=True)


async def test_cover_card_rejects_path_escape():
    with pytest.raises(ScriptError, match="outputs"):
        await run_script(
            SKILLS / "cover-card" / "scripts" / "render_cover.py",
            {
                "inputs": {"title": "越界", "output_dir": "../escape"},
                "output": {"subtitle": "x"},
            },
            SKILLS / "cover-card",
        )


async def test_format_check_flags_banned_words():
    result = await run_script(
        SKILLS / "quality-gate" / "scripts" / "format_check.py",
        {
            "inputs": {"text": "# 标题\n震惊！这才是唯一解"},
            "output": {"summary": "ok"},
        },
        SKILLS / "quality-gate",
    )
    assert result["check"]["passed"] is False
    assert any("震惊" in item["message"] for item in result["check"]["issues"])


async def test_format_check_reports_actual_line_number():
    result = await run_script(
        SKILLS / "quality-gate" / "scripts" / "format_check.py",
        {
            "inputs": {"text": "# 标题\n\n" + "字" * 181},
            "output": {"summary": "ok"},
        },
        SKILLS / "quality-gate",
    )
    messages = [item["message"] for item in result["check"]["issues"]]
    assert any("第 3 行" in message for message in messages)


async def test_keyword_scan_marks_two_hits_high():
    result = await run_script(
        SKILLS / "risk-scan" / "scripts" / "keyword_scan.py",
        {
            "inputs": {"text": "全网最低价，100%有效"},
            "output": {"summary": "high"},
        },
        SKILLS / "risk-scan",
    )
    assert result["scan"]["risk_level"] == "high"
    assert result["scan"]["total"] >= 2
    words = {hit["word"] for hit in result["scan"]["hits"]}
    assert {"全网最低", "100%有效"} <= words
