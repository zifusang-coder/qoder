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
        {"context": {"profile": True, "references": ["../../outside.md"]}},
        {"context": {"profile": "sometimes"}},
        {"postprocess": "../evil.py"},
        {"llm": {"tier": "turbo"}},
    ],
)
def test_invalid_manifest_is_rejected(tmp_path, changes):
    with pytest.raises(ManifestError):
        load_skill(_mutated_copy(tmp_path, **changes))


def test_profile_none_disables_profile(tmp_path):
    skill = load_skill(_mutated_copy(tmp_path, context={"profile": "none"}))
    assert skill.manifest.profile is False
