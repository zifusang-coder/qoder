from pathlib import Path

from app.skills.registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2] / "skills"
EXPECTED = {
    "topic-evaluator",
    "article-outline",
    "wechat-article",
    "xhs-note",
    "copywriting",
    "title-hook",
}


def test_builtin_skills_load_cleanly():
    registry = SkillRegistry(ROOT)
    assert registry.errors == []
    assert EXPECTED <= set(registry.skills)
