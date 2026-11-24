import uuid
from pathlib import Path

from app.config import get_settings
from app.skills.executor import declared_inputs
from app.skills.manifest import LoadedSkill


def adhoc_output_dir(tenant_id: str) -> Path:
    return get_settings().outputs_dir / "adhoc" / tenant_id / uuid.uuid4().hex


def prepare_inputs(skill: LoadedSkill, inputs: dict, tenant_id: str) -> dict:
    prepared = declared_inputs(skill, inputs)
    if "output_dir" in (skill.manifest.inputs.get("properties") or {}):
        # 服务端生成按租户隔离的单次运行目录，避免调用方写入任意路径
        prepared["output_dir"] = str(adhoc_output_dir(tenant_id))
    return prepared


def public_output_data(data: dict | None, outputs_root: Path) -> dict | None:
    """把 outputs 内的绝对路径转为相对路径，避免向客户端泄露部署目录。"""
    if not data or "path" not in data:
        return data
    path = Path(str(data["path"]))
    try:
        rel = path.resolve().relative_to(outputs_root.resolve())
    except ValueError:
        return data
    return {**data, "path": str(rel)}
