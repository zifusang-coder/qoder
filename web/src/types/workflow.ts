/** 图数据与后端 `app/workflow/graph.py` 的 JSON 形状一致。 */
export interface GraphNode {
  id: string;
  type: string;
  position?: { x: number; y: number };
  data: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface InputField {
  key: string;
  label?: string;
  type: "string" | "number" | "boolean";
  required?: boolean;
  maxLength?: number;
}

export interface Cadence {
  kind: "daily" | "weekly" | string;
  weekdays?: number[];
  time?: string;
  timezone?: string;
}

export interface WorkflowLastRun {
  id: string;
  status: string;
  created_at: string | null;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  created_by: string;
  draft_revision: number;
  has_unpublished: boolean;
  latest_version: number | null;
  next_run_at: string | null;
  last_run: WorkflowLastRun | null;
  created_at: string | null;
}

export interface WorkflowVersionSummary {
  id: string;
  version: number;
  source_revision: number;
  published_by: string;
  published_at: string;
  note: string;
}

export interface WorkflowVersionDetail extends WorkflowVersionSummary {
  graph: Graph;
}

export interface WorkflowSchedule {
  id: string;
  workflow_version_id: string;
  cadence: Cadence;
  inputs: Record<string, unknown>;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
}

export interface WorkflowDetail extends WorkflowSummary {
  draft_graph: Graph | null;
  latest_published: WorkflowVersionSummary | null;
  schedules: WorkflowSchedule[];
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_version_id: string;
  version: number;
  trigger: string;
  triggered_by: string | null;
  schedule_id: string | null;
  scheduled_for: string | null;
  status: string;
  inputs: Record<string, unknown>;
  result: Record<string, unknown>;
  error_code: string;
  error_message: string;
  usage: Partial<
    Record<
      | "nodes"
      | "steps"
      | "tool_calls"
      | "prompt_tokens"
      | "completion_tokens"
      | "elapsed_ms"
      | "paused_ms",
      number
    >
  >;
  attempts: number;
  resume_count: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowNodeRun {
  node_id: string;
  attempt: number;
  kind: string;
  status: string;
  tool_step_index: number | null;
  agent_run_id: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error: string;
  duration_ms: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowApproval {
  id: string;
  run_id: string;
  workflow_id: string;
  source: "workflow" | "agent" | string;
  kind: string;
  node_id: string;
  tool_name: string;
  risk: string;
  title: string;
  payload: Record<string, unknown>;
  status: string;
  note: string;
  expires_at: string | null;
  created_at: string | null;
}

export interface WorkflowRunDetail {
  run: WorkflowRun;
  node_runs: WorkflowNodeRun[];
  approvals: WorkflowApproval[];
  result: Record<string, unknown>;
}

export interface ValidationIssue {
  code: string;
  node_id?: string;
  message: string;
  severity?: string;
}

export interface ValidationResult {
  ok: boolean;
  issues: ValidationIssue[];
}

export interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  risk: string;
}

export interface TemplateSummary {
  key: string;
  name: string;
  description: string;
  parameters: Record<string, unknown>[];
}
