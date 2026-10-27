from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.skills.manifest import LoadedSkill, ManifestError, load_skill


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root
        self._skills: dict[str, LoadedSkill] = {}
        self._loaded = False

    def load(self) -> None:
        skills: dict[str, LoadedSkill] = {}
        if self.root.exists():
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir() or not (entry / "manifest.yaml").exists():
                    continue
                skill = load_skill(entry)
                skills[skill.manifest.id] = skill
        self._skills = skills
        self._loaded = True

    @property
    def skills(self) -> dict[str, LoadedSkill]:
        if not self._loaded:
            self.load()
        return self._skills

    def get(self, skill_id: str) -> LoadedSkill:
        skill = self.skills.get(skill_id)
        if skill is None:
            raise ManifestError(f"技能不存在：{skill_id}")
        return skill


@lru_cache
def get_registry() -> SkillRegistry:
    registry = SkillRegistry(get_settings().skills_dir)
    registry.load()
    return registry
