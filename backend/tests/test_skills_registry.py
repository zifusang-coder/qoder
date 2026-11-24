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
        {"llm": {"temperature": True}},
        {"cost_estimate_tokens": 1.9},
        {"postprocess_timeout": 0},
        {"postprocess_timeout": 601},
        {"postprocess_timeout": True},
        {"postprocess_timeout": "fast"},
    ],
)
def test_invalid_manifest_is_rejected(tmp_path, changes):
    with pytest.raises(ManifestError):
        load_skill(_mutated_copy(tmp_path, **changes))


def test_postprocess_timeout_is_configurable(tmp_path):
    skill = load_skill(_mutated_copy(tmp_path, postprocess_timeout=120))
    assert skill.manifest.postprocess_timeout == 120.0

    default = load_skill(FIXTURE / "demo-skill")
    assert default.manifest.postprocess_timeout == 30.0


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


def test_symlinked_references_dir_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    skill_dir = _mutated_copy(
        tmp_path,
        context={"profile": True, "references": ["secret.md"]},
    )
    (skill_dir / "references").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError, match="不能指向技能目录之外"):
        load_skill(skill_dir)


def test_references_and_postprocess_inside_skill_are_accepted(tmp_path):
    skill_dir = _mutated_copy(
        tmp_path,
        postprocess="scripts/check.py",
        context={"profile": True, "references": ["style.md"]},
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "style.md").write_text("风格", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "check.py").write_text(
        "print('{}')", encoding="utf-8"
    )
    skill = load_skill(skill_dir)
    assert skill.manifest.references == ["style.md"]
    assert skill.manifest.postprocess == "scripts/check.py"


def test_registry_skips_invalid_skill(tmp_path):
    shutil.copytree(FIXTURE / "demo-skill", tmp_path / "demo-skill")
    broken = tmp_path / "broken-skill"
    shutil.copytree(FIXTURE / "demo-skill", broken)
    manifest = broken / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["id"] = "broken-skill"
    manifest.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )
    (broken / "prompt.md").unlink()
    registry = SkillRegistry(tmp_path)
    registry.load()
    assert set(registry.skills) == {"demo-skill"}
    assert len(registry.errors) == 1
    with pytest.raises(ManifestError):
        registry.get("broken-skill")


def test_profile_none_disables_profile(tmp_path):
    skill = load_skill(_mutated_copy(tmp_path, context={"profile": "none"}))
    assert skill.manifest.profile is False
