import shutil
from pathlib import Path

import pytest
import yaml

from app.skills.manifest import ManifestError, load_skill
from app.skills.registry import SkillRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "skills"


def _mutated_copy(tmp_path: Path, **changes) -> Path:
    target = tmp_path / "demo-skill"
    shutil.copytree(FIXTURE / "demo-skill", target)
    manifest_path = target / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data.update(changes)
    manifest_path.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )
    return target


def test_load_demo_skill():
    skill = load_skill(FIXTURE / "demo-skill")
    assert skill.manifest.id == "demo-skill"
    assert skill.manifest.llm_tier == "fast"
    assert "测试助教" in skill.prompt


def test_registry_rejects_id_mismatch(tmp_path):
    target = tmp_path / "wrong-id"
    shutil.copytree(FIXTURE / "demo-skill", target)
    with pytest.raises(ManifestError):
        load_skill(target)


def test_registry_loads_all():
    registry = SkillRegistry(FIXTURE)
    registry.load()
    assert registry.get("demo-skill").manifest.name == "演示技能"


@pytest.mark.parametrize(
    "changes",
    [
        {"layer": "unknown"},
        {"tools": "llm"},
        {"tools": ["shell"]},
        {"context": {"profile": "sometimes"}},
        {"inputs": []},
        {"context": []},
        {"outputs": []},
        {"llm": []},
        {"llm": {"tier": "turbo"}},
        {"cost_estimate_tokens": 1.9},
    ],
)
def test_invalid_manifest_is_rejected(tmp_path, changes):
    with pytest.raises(ManifestError):
        load_skill(_mutated_copy(tmp_path, **changes))


def test_reference_outside_skill_dir_is_rejected(tmp_path):
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    skill_dir = _mutated_copy(
        tmp_path,
        context={"profile": True, "references": ["../../outside.md"]},
    )
    with pytest.raises(ManifestError, match="不能指向技能目录之外"):
        load_skill(skill_dir)


def test_postprocess_outside_skill_dir_is_rejected(tmp_path):
    (tmp_path / "evil.py").write_text("print('x')", encoding="utf-8")
    skill_dir = _mutated_copy(tmp_path, postprocess="../evil.py")
    with pytest.raises(ManifestError, match="不能指向技能目录之外"):
        load_skill(skill_dir)


def test_profile_none_disables_profile(tmp_path):
    skill = load_skill(_mutated_copy(tmp_path, context={"profile": "none"}))
    assert skill.manifest.profile is False
