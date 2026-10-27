from pathlib import Path

from app.skills.registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2] / "skills"
EXPECTED_COUNT = 12  # Phase 1 内置技能目录数量，新增技能时同步更新验收清单
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
    assert len(registry.skills) == EXPECTED_COUNT
    assert EXPECTED <= set(registry.skills)
