import { canWrite, isAdmin, type Me, type Role } from "./auth";
import type { AgentRun } from "./agent";

export type TeamRole = "planner" | "scout" | "creator" | "critic" | "publisher";
export const EXECUTION_ROLES: TeamRole[] = ["scout", "creator", "critic", "publisher"];

const ROLE_LABELS: Record<string, string> = {
  planner: "Lead 规划", scout: "调研", creator: "创作",
  critic: "审稿", publisher: "交付",
};
const ENTRY_LABELS: Record<string, string> = {
  goal: "目标", plan: "计划", delegation: "委派", result: "产出",
  critic: "审稿", decision: "决策", final: "交付",
};

export interface TeamRoleConfig {
  role: TeamRole; label: string; enabled: boolean;
  tools: string[]; default_tools: string[];
  budget: Record<string, number>; limits: Record<string, number>;
}
export interface TeamChild {
  id: string; role: string; status: string; attempts: number;
  usage: Record<string, number>; result_text: string; error_code: string;
  created_at: string; finished_at: string | null;
}
export interface TeamEntry {
  seq: number; kind: string; role: string;
  content: Record<string, unknown>; created_at: string;
}
export interface TeamArtifact { kind: string; id: string; title: string; role?: string; seq?: number }
export interface TeamRunDetail {
  run: AgentRun; children: TeamChild[]; entries: TeamEntry[]; artifacts: TeamArtifact[];
}

export const TEAM_BUDGET_KEYS = ["max_tokens", "max_tool_calls", "max_turns", "max_seconds"] as const;
const TEAM_CEILINGS: Record<string, number> = {
  max_tokens: 200000, max_tool_calls: 60, max_turns: 20, max_seconds: 1800,
};

export function defaultTeamBudget(): Record<string, number> {
  return { max_tokens: 120000, max_tool_calls: 40, max_turns: 12, max_seconds: 900 };
}
export function roleLabel(role: string): string { return ROLE_LABELS[role] ?? role; }
export function entryKindLabel(kind: string): string { return ENTRY_LABELS[kind] ?? kind; }
export function canEditTeamRoles(role: Role): boolean { return isAdmin(role); }
export function canStartTeamRun(role: Role): boolean { return canWrite(role); }

/** 团队口径（spec §8）：admin/owner 可取消任意活跃团队运行，不同于单 Agent 仅发起人可取消。 */
export function canCancelTeamRun(
  run: Pick<AgentRun, "status" | "user_id">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  const live =
    run.status === "queued" ||
    run.status === "running" ||
    run.status === "waiting_approval";
  if (!live) return false;
  if (isAdmin(viewer.role)) return true;
  return viewer.id === run.user_id && canWrite(viewer.role);
}

export function validateParticipants(selected: string[], configs: TeamRoleConfig[]): string | null {
  if (selected.length === 0) return "至少选择一个执行角色";
  for (const role of selected) {
    if (!EXECUTION_ROLES.includes(role as TeamRole)) return `未知角色：${role}`;
    const config = configs.find((item) => item.role === role);
    if (config && !config.enabled) return `角色 ${roleLabel(role)} 已被关闭`;
  }
  if (!selected.includes("creator")) return "团队必须包含创作（creator）";
  return null;
}

export function validateTeamBudget(raw: Record<string, unknown>): string | null {
  for (const [key, value] of Object.entries(raw)) {
    if (!TEAM_BUDGET_KEYS.includes(key as (typeof TEAM_BUDGET_KEYS)[number]))
      return `未知预算维度：${key}`;
    if (typeof value !== "number" || !Number.isInteger(value) || value <= 0)
      return `预算 ${key} 必须是正整数`;
    if (value > TEAM_CEILINGS[key]) return `预算 ${key} 超过上限`;
  }
  return null;
}

export function teamTotals(children: TeamChild[]): { tokens: number; tool_calls: number } {
  let tokens = 0;
  let toolCalls = 0;
  for (const child of children) {
    const usage = child.usage ?? {};
    tokens += (Number(usage.prompt_tokens) || 0) + (Number(usage.completion_tokens) || 0);
    toolCalls += Number(usage.tool_calls) || 0;
  }
  return { tokens, tool_calls: toolCalls };
}