import { canWrite, isAdmin, type Me, type Role } from "./auth";

/**
 * 整条运行可能出现的状态；接口返回的是裸字符串，因此各标签函数都接受
 * `string` 并在遇到未知值时回退到原值。
 */
export type AgentStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface AgentApproval {
  id: string;
  run_id: string;
  step_id: string;
  tool_name: string;
  risk: string;
  payload: Record<string, unknown>;
  status: string;
  note: string;
  expires_at: string | null;
  decided_at: string | null;
}

export interface AgentStep {
  step_index: number;
  kind: string;
  tool_name: string;
  status: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error: string;
  duration_ms: number;
  started_at: string | null;
  finished_at: string | null;
}

/** 与后端 `RunOut` 对齐；`result.artifacts` 可能缺省，读取时要判空。 */
export interface AgentRun {
  id: string;
  user_id: string;
  goal: string;
  mode: string;
  status: string;
  plan: Record<string, unknown>;
  result_text: string;
  result: Record<string, unknown>;
  error_code: string;
  error_message: string;
  budget: Record<string, number>;
  usage: Record<string, number>;
  step_count: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  pending_approval: AgentApproval | null;
}

export interface AgentRunDetail extends AgentRun {
  steps: AgentStep[];
}

export interface StepGroup {
  llm: AgentStep | null;
  actions: AgentStep[];
}

export interface Artifact {
  type?: string;
  id?: string;
  title?: string;
}

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "待确认",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_STATUS_LABELS: Record<string, string> = {
  running: "执行中",
  ok: "成功",
  error: "出错",
  denied: "被拒绝",
  skipped: "已跳过",
  interrupted: "已中断",
};

const STEP_KIND_LABELS: Record<string, string> = {
  llm: "规划",
  tool: "工具",
  approval: "审批",
  note: "备注",
  final: "结论",
};

const ARTIFACT_LABELS: Record<string, string> = {
  project: "项目",
  content: "内容",
  media_job: "媒体任务",
  publish_job: "发布任务",
  profile: "画像",
  skill: "技能",
};

export function agentStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function stepStatusLabel(status: string): string {
  return STEP_STATUS_LABELS[status] ?? status;
}

export function stepKindLabel(kind: string): string {
  return STEP_KIND_LABELS[kind] ?? kind;
}

export function isRunActive(status: string): boolean {
  return status === "queued" || status === "running";
}

export function isRunTerminal(status: string): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/** 把步骤按「一次模型调用 + 它触发的动作」分组，顺序与输入一致。 */
export function groupSteps(steps: AgentStep[]): StepGroup[] {
  const groups: StepGroup[] = [];
  for (const step of steps) {
    if (step.kind === "llm") {
      groups.push({ llm: step, actions: [] });
      continue;
    }
    if (groups.length === 0) {
      groups.push({ llm: null, actions: [] });
    }
    groups[groups.length - 1].actions.push(step);
  }
  return groups;
}

export function canCreateRun(role: Role): boolean {
  return canWrite(role);
}

/** 审批只能由非发起人的管理员以上角色决定。 */
export function canDecideApproval(
  run: Pick<AgentRun, "status" | "user_id">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  return (
    run.status === "waiting_approval" &&
    isAdmin(viewer.role) &&
    viewer.id !== run.user_id
  );
}

/**
 * 只有发起人本人（且具备写权限）能取消活跃运行。后端对管理员取消他人的
 * 运行返回 403，所以这里不再给管理员额外开绿灯。
 */
export function canCancelRun(
  run: Pick<AgentRun, "status" | "user_id">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  const live =
    run.status === "queued" ||
    run.status === "running" ||
    run.status === "waiting_approval";
  return live && viewer.id === run.user_id && canWrite(viewer.role);
}

export function canRetryRun(status: string, role: Role): boolean {
  return canWrite(role) && (status === "failed" || status === "cancelled");
}

export function usageTokens(usage: Record<string, number>): number {
  return (Number(usage.prompt_tokens) || 0) + (Number(usage.completion_tokens) || 0);
}

export function budgetPercent(used: number, max: number): number {
  if (!Number.isFinite(max) || max <= 0) return 0;
  const raw = Math.round((used / max) * 100);
  return Math.min(100, Math.max(0, raw));
}

/** 运行时长的短标签；未开始的运行返回空串。 */
export function formatElapsed(
  startedAt: string | null,
  finishedAt: string | null,
  now: number = Date.now(),
): string {
  if (!startedAt) return "";
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return "";
  const end = finishedAt ? Date.parse(finishedAt) : now;
  if (Number.isNaN(end)) return "";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
  }
  return `${Math.floor(seconds / 3600)}h${Math.floor((seconds % 3600) / 60)}m`;
}

export function artifactLabel(type: string): string {
  return ARTIFACT_LABELS[type] ?? (type || "产物");
}

/** 产物跳转地址：项目/内容进项目详情，画像、发布、媒体回各自列表页。 */
export function artifactLink(artifact: Artifact): string {
  if (
    (artifact.type === "project" || artifact.type === "content") &&
    artifact.id
  ) {
    return `/projects/${encodeURIComponent(artifact.id)}`;
  }
  if (artifact.type === "profile") return "/profiles";
  if (artifact.type === "publish_job") return "/publish";
  return "/projects";
}

export function firstLine(text: string, limit = 60): string {
  const line =
    (text ?? "")
      .split("\n")
      .map((part) => part.trim())
      .find(Boolean) ?? "";
  return line.length > limit ? `${line.slice(0, limit)}…` : line;
}
