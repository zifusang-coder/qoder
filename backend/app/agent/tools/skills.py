import re
from pathlib import Path

import jsonschema

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import owned_project, parse_uuid, schema
from app.errors import AppError
from app.services.media import MEDIA_SKILLS
from app.services.outputs import prepare_inputs
from app.services.profiles import inputs_query, profile_block_for
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.runner import ScriptError

_ABSOLUTE_PATH_RE = re.compile(r"/[^\s\"'<>|,，。；、）)]+")


def _relative_to_root(candidate: str, outputs_root: Path) -> str | None:
    try:
        path = Path(candidate)
        if not path.is_absolute():
            return None
        rel = path.resolve().relative_to(outputs_root.resolve())
    except (OSError, ValueError):
        return None
    return "." if str(rel) == "." else str(rel)


def _sanitize_string(value: str, outputs_root: Path) -> str:
    direct = _relative_to_root(value, outputs_root)
    if direct is not None:
        return direct

    def replace(match: re.Match[str]) -> str:
        relative = _relative_to_root(match.group(0), outputs_root)
        return relative if relative is not None else match.group(0)

    return _ABSOLUTE_PATH_RE.sub(replace, value)


def _sanitize_outputs(value, outputs_root: Path):
    """递归去掉输出中的部署绝对路径，仅保留 outputs 目录下的相对路径。"""
    if isinstance(value, dict):
        return {key: _sanitize_outputs(item, outputs_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_outputs(item, outputs_root) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, outputs_root)
    return value


async def run_skill(ctx: ToolContext, args: dict) -> ToolResult:
    skill_id = str(args.get("skill_id") or "")
    if skill_id in MEDIA_SKILLS:
        # 媒体技能产物必须经 MediaJob 落库并登记 Content，不能在工具调用里同步跑
        raise AppError("invalid_arguments", "媒体技能请使用 media.generate", 422)
    try:
        skill = ctx.registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    await require_enabled(ctx.session, ctx.tenant_id, skill_id)
    raw_inputs = args.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        raise AppError("invalid_arguments", "inputs 必须是对象", 422)
    inputs = prepare_inputs(skill, dict(raw_inputs), ctx.tenant_id)
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError("invalid_arguments", "技能输入参数校验失败", 422) from exc
    profile_id = None
    if args.get("profile_id"):
        profile_id = parse_uuid(args["profile_id"], "账号画像不存在")
    if args.get("project_id"):
        project = await owned_project(
            ctx.session, ctx.tenant_id, args["project_id"]
        )
        profile_id = profile_id or project.profile_id
    profile_block = ""
    if profile_id:
        profile_block = await profile_block_for(
            ctx.session,
            profile_id,
            ctx.tenant_id,
            query=inputs_query(inputs),
        )
    if ctx.gateway is None:
        raise AppError("llm_not_configured", "未配置可用的大模型", 503)
    executor = SkillExecutor(
        registry=ctx.registry,
        gateway=ctx.gateway,
        session=ctx.session,
        tenant_id=ctx.tenant_id,
        profile_block=profile_block,
        created_by=ctx.user_id,
    )
    try:
        result = await executor.run(skill_id, inputs)
    except AppError:
        raise
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except jsonschema.ValidationError as exc:
        raise AppError("invalid_arguments", "技能输入参数校验失败", 422) from exc
    except ScriptError as exc:
        raise AppError("skill_script_failed", "后处理脚本执行失败", 500) from exc
    except ValueError as exc:
        raise AppError("skill_output_invalid", "模型输出格式异常", 502) from exc
    return ToolResult(
        summary=result.text[:2000],
        data={
            "skill_id": result.skill_id,
            "run_id": result.run_id,
            "text": result.text[:8000],
            "data": _sanitize_outputs(result.data, ctx.settings.outputs_dir),
            "llm_fallback": result.llm_fallback,
        },
        artifacts=[
            {
                "type": "skill_run",
                "id": result.run_id,
                "title": skill.manifest.name,
            }
        ],
    )


SPECS = [
    ToolSpec(
        name="skill.run",
        description="执行一个已启用的创作技能，返回文本与结构化结果。",
        input_schema=schema(
            {
                "skill_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "inputs": {"type": "object"},
                "profile_id": {"type": "string", "minLength": 1},
                "project_id": {"type": "string", "minLength": 1},
            },
            ("skill_id",),
        ),
        risk="low",
        handler=run_skill,
    )
]
