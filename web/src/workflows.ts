import { canWrite, isAdmin, type Me, type Role } from "./auth";
import type {
  Cadence,
  Graph,
  GraphNode,
  InputField,
  WorkflowApproval,
  WorkflowRun,
  WorkflowSchedule,
} from "./types/workflow";

const NODE_KIND_LABELS: Record<string, string> = {
  input: "输入",
  tool: "工具",
  agent: "Agent",
  condition: "条件",
  review: "审核",
  output: "输出",
};

const NODE_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  ok: "成功",
  error: "失败",
  denied: "被拒绝",
  skipped: "已跳过",
  interrupted: "已中断",
};

const NODE_STATUS_BADGES: Record<string, string> = {
  queued: "badge bg-line text-ink-500",
  running: "badge bg-sage-50 text-sage-700",
  ok: "badge bg-sage-50 text-sage-700",
  error: "badge bg-wine-50 text-wine-700",
  denied: "badge bg-wine-50 text-wine-700",
  skipped: "badge bg-line text-ink-300",
  interrupted: "badge bg-surface text-warm-600 border border-line",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "待审核",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动",
  schedule: "定时",
};

const SOURCE_LABELS: Record<string, string> = {
  workflow: "工作流审核",
  agent: "Agent 工具确认",
};

const WEEKDAY_LABELS: Record<number, string> = {
  1: "一",
  2: "二",
  3: "三",
  4: "四",
  5: "五",
  6: "六",
  7: "日",
};

export function nodeKindLabel(kind: string): string {
  return NODE_KIND_LABELS[kind] ?? kind;
}

export function nodeStatusLabel(status: string): string {
  return NODE_STATUS_LABELS[status] ?? status;
}

export function nodeStatusBadge(status: string): string {
  return NODE_STATUS_BADGES[status] ?? "badge bg-line text-ink-500";
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status;
}

export function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? trigger;
}

export function approvalSourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export function nodeLabel(node: GraphNode): string {
  const label = node.data.label;
  return typeof label === "string" && label.trim() ? label : node.id;
}

function isInputField(value: unknown): value is InputField {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.key === "string" && record.key.length > 0;
}

export function inputFields(graph: Graph): InputField[] {
  const input = graph.nodes.find((node) => node.type === "input");
  const fields = input?.data.fields;
  if (!Array.isArray(fields)) return [];
  return fields.filter(isInputField);
}

/** 空图给编辑器一个可拖拽的起点；连线由用户补。 */
export function emptyGraph(): Graph {
  return {
    nodes: [
      {
        id: "in",
        type: "input",
        position: { x: 80, y: 160 },
        data: { label: "输入" },
      },
      {
        id: "out",
        type: "output",
        position: { x: 640, y: 160 },
        data: { label: "输出" },
      },
    ],
    edges: [],
  };
}

export function upstreamNodeIds(graph: Graph, nodeId: string): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const list = incoming.get(edge.target);
    if (list) list.push(edge.source);
    else incoming.set(edge.target, [edge.source]);
  }
  const seen = new Set<string>();
  const queue = [...(incoming.get(nodeId) ?? [])];
  while (queue.length > 0) {
    const id = queue.pop()!;
    if (id === nodeId || seen.has(id)) continue;
    seen.add(id);
    queue.push(...(incoming.get(id) ?? []));
  }
  return seen;
}

export function topoOrder(graph: Graph): string[] | null {
  const ids = graph.nodes.map((node) => node.id);
  const indegree = new Map<string, number>(ids.map((id) => [id, 0]));
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) continue;
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
    const list = outgoing.get(edge.source);
    if (list) list.push(edge.target);
    else outgoing.set(edge.source, [edge.target]);
  }
  const queue = ids.filter((id) => indegree.get(id) === 0);
  const order: string[] = [];
  while (queue.length > 0) {
    const id = queue.shift()!;
    order.push(id);
    for (const next of outgoing.get(id) ?? []) {
      const left = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, left);
      if (left === 0) queue.push(next);
    }
  }
  return order.length === ids.length ? order : null;
}

export function conditionTargets(
  graph: Graph,
  nodeId: string,
): { yes: string | null; no: string | null } {
  const yes = graph.edges.find(
    (edge) => edge.source === nodeId && edge.sourceHandle === "true",
  );
  const no = graph.edges.find(
    (edge) => edge.source === nodeId && edge.sourceHandle === "false",
  );
  return { yes: yes?.target ?? null, no: no?.target ?? null };
}

/** 从输入节点出发，条件节点只沿被选中的句柄前进。 */
export function reachableNodes(
  graph: Graph,
  decisions: Record<string, boolean>,
): Set<string> {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const input = graph.nodes.find((node) => node.type === "input");
  const seen = new Set<string>();
  if (!input) return seen;
  const queue = [input.id];
  while (queue.length > 0) {
    const id = queue.pop()!;
    if (seen.has(id)) continue;
    seen.add(id);
    const node = byId.get(id);
    if (node?.type === "condition") {
      const targets = conditionTargets(graph, id);
      const next = decisions[id] ? targets.yes : targets.no;
      if (next) queue.push(next);
      continue;
    }
    for (const edge of graph.edges) {
      if (edge.source === id) queue.push(edge.target);
    }
  }
  return seen;
}

export interface VariableOption {
  value: string;
  label: string;
}

/** 属性面板的变量提示：输入字段 + 全部上游节点的整体输出与文本。 */
export function variableCandidates(
  graph: Graph,
  nodeId: string,
  fields: InputField[] = [],
): VariableOption[] {
  const options: VariableOption[] = fields.map((field) => ({
    value: `{{inputs.${field.key}}}`,
    label: `输入 · ${field.label || field.key}`,
  }));
  const upstream = upstreamNodeIds(graph, nodeId);
  for (const node of graph.nodes) {
    if (!upstream.has(node.id) || node.type === "input" || node.type === "output") {
      continue;
    }
    const name = nodeLabel(node);
    options.push({
      value: `{{nodes.${node.id}.output}}`,
      label: `${name} · 整体输出`,
    });
    options.push({
      value: `{{nodes.${node.id}.output.result_text}}`,
      label: `${name} · 文本`,
    });
  }
  return options;
}

export function canEditWorkflow(role: Role): boolean {
  return isAdmin(role);
}

export function canRunWorkflow(role: Role, hasPublished: boolean): boolean {
  return canWrite(role) && hasPublished;
}

export function canDecideWorkflowApproval(
  approval: Pick<WorkflowApproval, "status">,
  run: Pick<WorkflowRun, "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  if (approval.status !== "pending" || !isAdmin(viewer.role)) return false;
  if (run.trigger === "schedule") return true;
  return viewer.id !== run.triggered_by;
}

function controlsRun(
  run: Pick<WorkflowRun, "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  if (viewer.id === run.triggered_by) return true;
  return run.trigger === "schedule" && isAdmin(viewer.role);
}

export function canCancelWorkflowRun(
  run: Pick<WorkflowRun, "status" | "trigger" | "triggered_by">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  const live =
    run.status === "queued" ||
    run.status === "running" ||
    run.status === "waiting_approval";
  return live && controlsRun(run, viewer);
}

export function canResumeWorkflowRun(
  run: Pick<WorkflowRun, "status" | "trigger" | "triggered_by" | "resume_count">,
  viewer: Pick<Me, "id" | "role">,
): boolean {
  return run.status === "failed" && run.resume_count < 10 && controlsRun(run, viewer);
}

export function isRunTerminal(status: string): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

export function scheduleText(schedule: { cadence: Cadence }): string {
  const cadence = schedule.cadence;
  const time = typeof cadence.time === "string" ? cadence.time : "";
  const zone = cadence.timezone ? `（${cadence.timezone}）` : "";
  if (cadence.kind === "weekly") {
    const days = (cadence.weekdays ?? [])
      .slice()
      .sort((a, b) => a - b)
      .map((day) => WEEKDAY_LABELS[day] ?? String(day))
      .join("、");
    return `每周${days} ${time}${zone}`;
  }
  return `每天 ${time}${zone}`;
}

export type ScaleLevel = "ok" | "warn" | "over";

export interface ScaleHint {
  level: ScaleLevel;
  text: string;
}

/** 与后端发布校验一致：节点 ≤50、边 ≤100；90% 起提示，超过上限才视为超限。 */
export function graphScale(graph: Graph): ScaleHint {
  const nodes = graph.nodes.length;
  const edges = graph.edges.length;
  const level: ScaleLevel =
    nodes > 50 || edges > 100 ? "over" : nodes >= 45 || edges >= 90 ? "warn" : "ok";
  return { level, text: `${nodes}/50 节点 · ${edges}/100 边` };
}
