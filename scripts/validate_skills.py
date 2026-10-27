#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.skills.manifest import ManifestError, load_skill  # noqa: E402


def main() -> int:
    root = ROOT / "skills"
    errors: list[str] = []
    loaded = 0
    for entry in sorted(root.iterdir()) if root.exists() else []:
        if not entry.is_dir() or not (entry / "manifest.yaml").exists():
            continue
        try:
            skill = load_skill(entry)
            loaded += 1
            print(f"OK   {skill.manifest.id:<24} {skill.manifest.name}")
        except ManifestError as exc:
            errors.append(str(exc))
            print(f"FAIL {entry.name}: {exc}")
    print(f"\n共加载 {loaded} 个技能，错误 {len(errors)} 个")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
