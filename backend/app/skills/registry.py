import logging
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.skills.manifest import LoadedSkill, ManifestError, load_skill

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root
        self._skills: dict[str, LoadedSkill] = {}
        self._errors: list[str] = []
        self._loaded = False

    def load(self) -> None:
        skills: dict[str, LoadedSkill] = {}
        errors: list[str] = []
        if self.root.exists():
            for entry in sorted(self.root.iterdir()):
                if not entry.is_dir() or not (entry / "manifest.yaml").exists():
                    continue
                try:
                    skill = load_skill(entry)
                except ManifestError as exc:
                    # 单个坏技能不应让全部技能不可用；错误保留给校验脚本/日志
                    errors.append(str(exc))
                    logger.warning("技能加载失败：%s", exc)
                    continue
                skills[skill.manifest.id] = skill
        self._skills = skills
        self._errors = errors
        self._loaded = True

    @property
    def skills(self) -> dict[str, LoadedSkill]:
        if not self._loaded:
            self.load()
        return dict(self._skills)

    @property
    def errors(self) -> list[str]:
        if not self._loaded:
            self.load()
        return list(self._errors)

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
