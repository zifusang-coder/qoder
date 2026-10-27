# Phase 0 验收清单

验收日期：2026-10-27。环境：macOS 本地；后端 Python 3.11.15（uv）；前端 Node/npm；Docker Compose 项目 `newmedia`。

- [x] `docker compose ps` 中 db 为 healthy
- [x] redis 容器为 Up，且 `docker compose exec -T redis redis-cli ping` 返回 `PONG`（compose 未定义 redis healthcheck，因此 `ps` 不显示 healthy）
- [x] `cd backend && uv run alembic upgrade head` 成功；`downgrade base` 可回滚；再次 `upgrade head` 成功（round-trip 后重启应用，默认管理员重新种入）
- [x] `GET /health` 返回 `{"status":"ok"}`（HTTP 200）
- [x] 默认管理员可登录（HTTP 200，返回 `access_token`）；错误密码返回 401，body `code=invalid_credentials`
- [x] 未带 token 请求 `/api/auth/me` 返回 401（`code=unauthorized`）
- [x] 保存 LLM 提供商后列表仅显示掩码密钥（`sk-****5678`）；创建响应与列表响应均不含原始密钥；临时提供商已删除，列表恢复为空
- [x] 前端可登录、可见设置页与对话页：Task 0.7 已做浏览器验证；本任务以 `npm run test`（11 passed）与 `npm run build`（成功）回归。对话页当前为占位壳，尚无 `/api/chat` 接口
- [x] `cd backend && uv run pytest -q`：69 passed；`cd web && npm run test`：11 passed；`cd web && npm run build`：成功
