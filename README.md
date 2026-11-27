# 云AI新媒体运营系统

云AI新媒体运营系统覆盖「画像 → 创作 → 审稿 → 发布 → 数据复盘」闭环，并在其上提供三层 AI
原生能力（Assistant / Workflow / Team）与多租户 SaaS 基础。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [设计文档](docs/原生AI新媒体运营系统-开发文档.md) | 总体设计与领域模型 |
| [运维与配置手册](docs/ops.md) | 本地开发、worker、存储、pgvector、媒体、Agent/工作流/团队配置、生产部署、可观测性、测试 |
| [平台能力矩阵](docs/platforms.md) | 9 个平台的自动发布/手动导出/待接入状态与接入路线 |
| [技能开发与治理](skills/README.md) | 技能 manifest 约定与新增技能检查清单 |
| [Phase 0](docs/phase-0-checklist.md) · [Phase 1](docs/phase-1-checklist.md) · [Phase 3](docs/phase-3-checklist.md) | 阶段验收清单 |

## 核心能力

- **创作与发布**：账号画像与记忆、技能创作与一稿多改、项目内容版本、发布前检查/审核/排期、发布数据导入与复盘；9 个平台按能力矩阵区分自动发布、素材包导出与待接入，不做假成功。
- **AI 助理（第 1 层）**：工具调用运行时、预算与配额、租户策略审批、断点恢复；对话与运行均通过 SSE 流式下发。
- **工作流（第 2 层）**：画布编排与 DAG 并行/汇合、版本与草稿、定时触发、审批挂起与从失败处续跑。
- **协作团队（第 3 层）**：Planner / Scout / Creator / Critic / Publisher 五角色，黑板共享上下文、Critic 强制审稿与返工上限、团队预算收窄。
- **多租户 SaaS**：四级角色（viewer < editor < admin < owner）、租户生命周期（含停用后确认删除并清空租户数据）、项目/内容/画像/账号/成员删除、会话版本失效、审计与归档、登录限流、强制首次改密。
- **套餐与账单**：平台管理员维护可配置套餐并指派给租户；按北京时间自然月分项计量输入/输出 token（运行次数与媒体用量为指标），关账生成不可变账单，支持重开重算；不含在线支付。
- **开放能力**：管理员签发租户级 API Key（四项范围），外部脚本通过 `/api/public` 触发 Agent/工作流并读取运行；密钥只存哈希、创建时显示一次，每次调用写审计。
- **工程化**：CI（lint、后端/前端测试、空库迁移、Playwright 冒烟）、请求 ID 与日志关联、Prometheus 指标、失败告警、容器化生产编排。

## 快速开始

前置：Docker / Docker Compose v2、GNU Make、`uv`、Node.js `^20.19.0 || >=22.12.0`；端口
8000 / 5173 / 15432 / 16379 需空闲。完整说明见 [docs/ops.md §1](docs/ops.md#1-本地开发)。

```bash
make setup       # 复制 .env（若不存在）并安装后端依赖
make up          # PostgreSQL + Redis
make migrate     # alembic upgrade head
make dev         # 后端 http://localhost:8000
```

另开终端：

```bash
cd web && npm install && npm run dev   # 前端 http://localhost:5173
```

本地默认账号取自 `.env`（模板默认 `admin@local` / `admin12345`）。`ENV` 非 `dev` 时应用拒绝占位符
密钥并启动失败。

## 测试

```bash
make test                       # 后端：uv run pytest -q
npm --prefix web run test       # 前端单测（vitest）
npm --prefix web run build      # 类型检查 + 构建
npm --prefix web run test:e2e   # Playwright 冒烟（独立 8001/5321 端口与 newmedia_e2e 库）
```

真实模型用例默认跳过，`RUN_AGENT_E2E=1` / `RUN_WORKFLOW_E2E=1` / `RUN_TEAM_E2E=1` 分别启用。
细节见 [docs/ops.md §11](docs/ops.md#11-测试与验收)。

## 生产部署

```bash
make docker-build
make docker-up      # 迁移 → api/worker → web，全部健康检查通过后返回
make docker-down    # 停止容器，保留 pgdata 与 outputs 卷
```

需要 `.env` 中真实的 `JWT_SECRET`、`MASTER_KEY`、`ADMIN_PASSWORD`。对外入口为
`http://<host>:8080`，nginx 反代 `/api` 并保持 SSE 实时性。运维细节见
[docs/ops.md §9](docs/ops.md#9-生产部署) 与 [§10 可观测性](docs/ops.md#10-可观测性)。

## 约定

- 发布默认人工审核，LLM 不直接触发发布；AI 侧只能创建草稿任务。
- 平台密钥加密存储，接口只返回掩码；原始凭据不出现在日志与响应体。
- 不使用逆向接口、抓包重放或自动化点击绕过平台限制。
