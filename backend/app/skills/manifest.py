import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALLOWED_LAYERS = {"discover", "plan", "produce", "publish", "attribute", "general"}
ALLOWED_TOOLS = {"llm", "http", "script"}
ALLOWED_TIERS = {"fast", "standard", "premium"}
ALLOWED_OUTPUT_FORMATS = {"markdown", "json"}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SkillManifest:
    id: str
    name: str
    version: str
    layer: str
    description: str
    inputs: dict = field(default_factory=dict)
    profile: bool = True
    references: list[str] = field(default_factory=list)
    output_format: str = "markdown"
    output_schema: dict | None = None
    llm_tier: str = "standard"
    temperature: float = 0.7
    tools: list[str] = field(default_factory=lambda: ["llm"])
    postprocess: str | None = None
    cost_estimate_tokens: int = 0
    dir: Path = Path(".")


@dataclass(frozen=True)
class LoadedSkill:
    manifest: SkillManifest
    prompt: str


def _require(data: dict, key: str, skill_id: str):
    if key not in data:
        raise ManifestError(f"{skill_id}: manifest 缺少字段 {key}")
    return data[key]


def _string_list(value, field_name: str, skill_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{skill_id}: {field_name} 必须是字符串列表")
    return list(value)


def _inside(skill_dir: Path, relative: str, field_name: str, skill_id: str) -> Path:
    root = skill_dir.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ManifestError(f"{skill_id}: {field_name} 不能指向技能目录之外") from None
    return target


def _profile_flag(value, skill_id: str) -> bool:
    # 兼容设计文档的 required/optional/none 与布尔写法
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ManifestError(f"{skill_id}: context.profile 非法取值 {value!r}")
    if value in {"required", "optional"}:
        return True
    if value == "none":
        return False
    raise ManifestError(f"{skill_id}: context.profile 非法取值 {value!r}")


def load_skill(skill_dir: Path) -> LoadedSkill:
    skill_dir = skill_dir.resolve()
    manifest_path = skill_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise ManifestError(f"{skill_dir.name}: 缺少 manifest.yaml")
    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_manifest) or {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{skill_dir.name}: manifest.yaml 无法读取") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{skill_dir.name}: manifest.yaml 不是合法 YAML") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{skill_dir.name}: manifest.yaml 顶层必须是对象")

    skill_id = str(_require(data, "id", skill_dir.name))
    if skill_id != skill_dir.name:
        raise ManifestError(f"{skill_dir.name}: id 必须与目录名一致（当前 {skill_id}）")

    name = str(_require(data, "name", skill_id)).strip()
    if not name:
        raise ManifestError(f"{skill_id}: name 不能为空")

    layer = str(_require(data, "layer", skill_id))
    if layer not in ALLOWED_LAYERS:
        raise ManifestError(f"{skill_id}: 非法 layer {layer}")

    inputs = data.get("inputs")
    if inputs is None:
        inputs = {"type": "object", "properties": {}}
    if not isinstance(inputs, dict):
        raise ManifestError(f"{skill_id}: inputs 必须是 JSON Schema 对象")

    tools = _string_list(data.get("tools", ["llm"]), "tools", skill_id)
    unknown = set(tools) - ALLOWED_TOOLS
    if unknown:
        raise ManifestError(f"{skill_id}: 不允许的工具 {sorted(unknown)}")

    context = data.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ManifestError(f"{skill_id}: context 必须是对象")
    references = _string_list(
        context.get("references"), "context.references", skill_id
    )
    for ref in references:
        target = _inside(
            skill_dir, f"references/{ref}", "引用文件", skill_id
        )
        if not target.is_file():
            raise ManifestError(f"{skill_id}: 引用文件不存在 references/{ref}")

    postprocess = data.get("postprocess")
    if postprocess is not None:
        if not isinstance(postprocess, str) or not postprocess.strip():
            raise ManifestError(f"{skill_id}: postprocess 必须是非空路径字符串")
        if not _inside(skill_dir, postprocess, "postprocess", skill_id).is_file():
            raise ManifestError(f"{skill_id}: 后处理脚本不存在 {postprocess}")

    prompt_path = skill_dir / "prompt.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ManifestError(f"{skill_id}: prompt.md 无法读取") from None
    if not prompt.strip():
        raise ManifestError(f"{skill_id}: prompt.md 为空")

    outputs = data.get("outputs")
    if outputs is None:
        outputs = {}
    if not isinstance(outputs, dict):
        raise ManifestError(f"{skill_id}: outputs 必须是对象")
    output_format = str(outputs.get("format", "markdown"))
    if output_format not in ALLOWED_OUTPUT_FORMATS:
        raise ManifestError(f"{skill_id}: 非法输出格式 {output_format}")
    output_schema = outputs.get("schema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ManifestError(f"{skill_id}: outputs.schema 必须是对象")

    llm = data.get("llm")
    if llm is None:
        llm = {}
    if not isinstance(llm, dict):
        raise ManifestError(f"{skill_id}: llm 必须是对象")
    tier = str(llm.get("tier", "standard"))
    if tier not in ALLOWED_TIERS:
        raise ManifestError(f"{skill_id}: 非法 llm.tier {tier}")
    try:
        raw_temperature = llm.get("temperature", 0.7)
        if isinstance(raw_temperature, bool):
            raise TypeError
        temperature = float(raw_temperature)
    except (TypeError, ValueError):
        raise ManifestError(f"{skill_id}: llm.temperature 必须是数字") from None
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ManifestError(f"{skill_id}: llm.temperature 必须在 0-2 之间")

    cost = data.get("cost_estimate_tokens", 0)
    if isinstance(cost, bool) or not isinstance(cost, int):
        raise ManifestError(f"{skill_id}: cost_estimate_tokens 必须是整数")
    if cost < 0:
        raise ManifestError(f"{skill_id}: cost_estimate_tokens 不能为负")

    manifest = SkillManifest(
        id=skill_id,
        name=name,
        version=str(data.get("version", "0.1.0")),
        layer=layer,
        description=str(data.get("description", "")),
        inputs=inputs,
        profile=_profile_flag(context.get("profile", True), skill_id),
        references=references,
        output_format=output_format,
        output_schema=output_schema,
        llm_tier=tier,
        temperature=temperature,
        tools=tools,
        postprocess=postprocess,
        cost_estimate_tokens=cost,
        dir=skill_dir,
    )
    return LoadedSkill(manifest=manifest, prompt=prompt)
