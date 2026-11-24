# 技能开发与治理

每个技能是一个目录，包含 `manifest.yaml`（声明）与 `prompt.md`（系统提示词）。
加载与校验逻辑见 `backend/app/skills/manifest.py`，注册表在运行时扫描 `SKILLS_DIR`。

## 目录结构

```text
skills/<skill-id>/
├── manifest.yaml        # 必填：id 必须与目录名一致
├── prompt.md            # 必填：非空
├── references/          # 可选：manifest.context.references 引用的文件
└── scripts/             # 可选：postprocess 脚本（或复用 skills/shared/scripts）
```

## manifest 字段

| 字段 | 约束 |
| --- | --- |
| `id` | 必填，必须等于目录名 |
| `name` / `version` / `description` | 必填，名称非空 |
| `layer` | 必填，取值 `discover` / `plan` / `produce` / `publish` / `attribute` / `general` |
| `inputs` | 必填，JSON Schema 对象；`required` 与 `properties` 描述调用参数 |
| `context.profile` | `true`/`false`/`required`/`optional`，决定是否注入账号画像 |
| `context.references` | 字符串列表，只能指向技能目录内 `references/` 下存在的文件 |
| `postprocess` | 可选，技能目录内脚本路径；以子进程运行，环境变量白名单、默认 30 秒超时、无 shell |
| `postprocess_timeout` | 可选，1–600 秒 |
| `outputs.format` | `markdown` 或 `json`；`json` 时用 `outputs.schema` 校验 |
| `llm.tier` | `fast` / `standard` / `premium` |
| `tools` | `llm` / `http` / `script` 的子集 |
| `cost_estimate_tokens` | 用于成本展示的估算值 |

## 新增技能检查清单

- [ ] 目录名与 `id` 一致，`manifest.yaml` 能通过注册表加载（启动日志无 `ManifestError`）。
- [ ] `inputs` 是合法 JSON Schema，必填参数已列出，默认值写在 `properties` 中。
- [ ] `prompt.md` 说明角色、输入约束与输出格式，不包含任何真实密钥、内网地址或个人信息。
- [ ] 输出格式与 `outputs.format` 一致；`json` 输出必须有 `outputs.schema`。
- [ ] 需要画像时显式设置 `context.profile`，引用的文件都放在 `references/` 且存在。
- [ ] 后处理脚本只读声明的输入、只写输出目录，不访问网络凭据；超时内可完成。
- [ ] 至少有一个正常输入测试；涉及后处理时补一个失败/超时用例。
- [ ] 真实模型验收用例默认跳过（通过 `RUN_*_E2E` 开关启用），不阻断 CI。
- [ ] 若技能产出媒体文件，扩展名在项目媒体白名单内（`mp4/mp3/png/jpg/webp/srt/txt`）。
- [ ] 文档（README / docs/ops.md）与实际依赖保持一致。

## 治理原则

- 技能只能调用注册表允许的工具；新增工具类别需要改 `ALLOWED_TOOLS` 并补策略测试。
- 后处理是「受限子进程」而非 OS 级隔离；多租户生产环境要把执行迁到容器或沙箱后再开放第三方技能。
- 不把发布、排期等高风险动作写进技能；发布任务只能创建为草稿。
- 技能输出入库前统一脱敏与截断，提示词与中间产物不得包含原始凭据。
