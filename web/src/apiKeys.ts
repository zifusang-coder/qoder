export type ApiKeyScope =
  | "agent:run"
  | "agent:read"
  | "workflow:run"
  | "workflow:read";

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_by: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type ApiKeyCreated = ApiKey & { secret: string };

export const API_KEY_SCOPES: {
  value: ApiKeyScope;
  label: string;
  hint: string;
}[] = [
  { value: "agent:run", label: "创建 Agent 运行", hint: "提交目标并排队执行" },
  { value: "agent:read", label: "读取 Agent 运行", hint: "查询状态与步骤" },
  { value: "workflow:run", label: "触发工作流", hint: "按已发布版本运行" },
  { value: "workflow:read", label: "读取工作流运行", hint: "查询状态与节点" },
];

const SCOPE_LABELS: Record<string, string> = {
  "agent:run": "创建 Agent 运行",
  "agent:read": "读取 Agent 运行",
  "workflow:run": "触发工作流",
  "workflow:read": "读取工作流运行",
};

export function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope] ?? scope;
}

export function describeScopes(scopes: string[]): string {
  if (scopes.length === 0) return "无范围";
  return scopes.map(scopeLabel).join("、");
}

export function isKeyActive(key: Pick<ApiKey, "revoked_at">): boolean {
  return key.revoked_at === null;
}

export function formatKeyTime(value: string | null): string {
  if (!value) return "从未使用";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
