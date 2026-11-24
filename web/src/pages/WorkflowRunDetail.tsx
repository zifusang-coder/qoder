import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  artifactLabel,
  artifactLink,
  formatElapsed,
  usageTokens,
  type Artifact,
} from "../agent";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { useLiveSnapshot } from "../live";
import { errorText } from "../types";
import type {
  WorkflowApproval,
  WorkflowRunDetail as RunDetail,
  WorkflowVersionDetail,
} from "../types/workflow";
import {
  approvalSourceLabel,
  canCancelWorkflowRun,
  canDecideWorkflowApproval,
  canResumeWorkflowRun,
  executedEdgeIds,
  latestNodeStatuses,
  nodeKindLabel,
  nodeStatusLabel,
  runStatusBadge,
  runStatusLabel,
  triggerLabel,
} from "../workflows";
import WorkflowCanvas from "../components/workflow/WorkflowCanvas";

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function WorkflowRunDetail() {
  const { id = "", runId = "" } = useParams();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const [error, setError] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const detailQuery = useQuery({
    queryKey: ["workflow-run", runId],
    queryFn: () => api<RunDetail>(`/workflow-runs/${runId}`),
    enabled: Boolean(runId),
  });
  const detail = detailQuery.data ?? null;
  const run = detail?.run ?? null;

  const graphQuery = useQuery({
    queryKey: ["workflow-version", id, run?.version],
    queryFn: () =>
      api<WorkflowVersionDetail>(`/workflows/${id}/versions/${run!.version}`),
    enabled: Boolean(id && run?.version),
  });
  const graph = graphQuery.data?.graph ?? null;

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["workflow-run", runId] });
    void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
    void queryClient.invalidateQueries({ queryKey: ["workflows"] });
  }

  useLiveSnapshot<RunDetail>(
    ["workflow-run", runId],
    runId ? `/workflow-runs/${runId}/stream` : null,
    { onSettled: refresh },
  );

  const cancel = useMutation({
    mutationFn: () =>
      api<unknown>(`/workflow-runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: refresh,
    onError: (err) => setError(errorText(err)),
  });

  const resume = useMutation({
    mutationFn: () =>
      api<unknown>(`/workflow-runs/${runId}/resume`, { method: "POST" }),
    onSuccess: refresh,
    onError: (err) => setError(errorText(err)),
  });

  const decide = useMutation({
    mutationFn: (input: {
      approval: WorkflowApproval;
      decision: "approve" | "reject";
      note: string;
    }) => {
      const path =
        input.approval.source === "agent"
          ? `/agent/approvals/${input.approval.id}/decide`
          : `/workflow-approvals/${input.approval.id}/decide`;
      return api<unknown>(path, {
        method: "POST",
        body: JSON.stringify({ decision: input.decision, note: input.note }),
      });
    },
    onSuccess: (_, input) => {
      setNotes((current) => ({ ...current, [input.approval.id]: "" }));
      setError("");
      refresh();
    },
    onError: (err) => setError(errorText(err)),
  });

  if (detailQuery.isError) {
    return (
      <div className="space-y-3">
        <Link className="text-sm text-sage-700" to={`/workflows/${id}`}>
          ← 返回工作流
        </Link>
        <p className="text-sm text-wine-700">{errorText(detailQuery.error)}</p>
      </div>
    );
  }

  if (!run || !detail) {
    return <p className="text-sm text-ink-500">加载运行详情…</p>;
  }

  const statuses = latestNodeStatuses(detail.node_runs);
  const executed = graph ? executedEdgeIds(graph, detail.node_runs) : new Set<string>();
  const artifacts = Array.isArray(detail.result?.artifacts)
    ? (detail.result.artifacts as Artifact[])
    : [];
  const canCancel = me ? canCancelWorkflowRun(run, me) : false;
  const canResume = me ? canResumeWorkflowRun(run, me) : false;

  const byNode = new Map<string, typeof detail.node_runs>();
  for (const item of detail.node_runs) {
    const list = byNode.get(item.node_id);
    if (list) list.push(item);
    else byNode.set(item.node_id, [item]);
  }

  return (
    <div className="space-y-4">
      <header className="card card-pad flex flex-wrap items-center gap-3">
        <Link className="btn-secondary" to={`/workflows/${id}`}>
          ← 返回工作流
        </Link>
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-semibold">
            运行 {run.id.slice(0, 8)} · v{run.version}
          </h1>
          <p className="text-xs text-ink-500">
            {triggerLabel(run.trigger)} · {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
          </p>
        </div>
        <span className={runStatusBadge(run.status)}>{runStatusLabel(run.status)}</span>
        {canCancel && (
          <button
            className="btn-danger"
            disabled={cancel.isPending}
            type="button"
            onClick={() => cancel.mutate()}
          >
            取消运行
          </button>
        )}
        {canResume && (
          <button
            className="btn-primary"
            disabled={resume.isPending || run.resume_count >= 10}
            title={run.resume_count >= 10 ? "已达到续跑上限（10 次）" : ""}
            type="button"
            onClick={() => resume.mutate()}
          >
            从失败处续跑
          </button>
        )}
      </header>

      {error && <p className="text-sm text-wine-700">{error}</p>}
      {run.error_code && (
        <div className="rounded-lg bg-wine-50 px-3 py-2 text-sm text-wine-700">
          <span className="font-medium">{run.error_code}</span>
          {run.error_message && <span> · {run.error_message}</span>}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-2">
          {graph ? (
            <WorkflowCanvas
              graph={graph}
              version={detailQuery.dataUpdatedAt}
              readOnly
              selectedId={selectedId}
              statuses={statuses}
              executedEdges={executed}
              onSelect={setSelectedId}
            />
          ) : (
            <p className="text-sm text-ink-500">加载版本图…</p>
          )}
          {graphQuery.isError && (
            <p className="text-sm text-wine-700">{errorText(graphQuery.error)}</p>
          )}
          {artifacts.length > 0 && (
            <section className="card card-pad space-y-2">
              <h2 className="text-sm font-semibold">产物</h2>
              <ul className="space-y-1 text-sm">
                {artifacts.map((artifact, index) => (
                  <li key={`${artifact.type}-${artifact.id ?? index}`}>
                    <Link className="text-sage-700" to={artifactLink(artifact)}>
                      {artifactLabel(artifact.type ?? "")}
                      {artifact.title ? ` · ${artifact.title}` : ""}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <aside className="space-y-3">
          <section className="card card-pad grid grid-cols-2 gap-2 text-sm">
            <div>
              <p className="text-xs text-ink-500">触发方式</p>
              <p>{triggerLabel(run.trigger)}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">耗时</p>
              <p>{formatElapsed(run.started_at, run.finished_at) || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">Tokens</p>
              <p>{usageTokens(run.usage)}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">节点尝试</p>
              <p>{detail.node_runs.length}</p>
            </div>
            <div>
              <p className="text-xs text-ink-500">续跑次数</p>
              <p>
                {run.resume_count}/10
              </p>
            </div>
            <div>
              <p className="text-xs text-ink-500">入参</p>
              <p className="truncate" title={pretty(run.inputs)}>
                {Object.keys(run.inputs).length} 个字段
              </p>
            </div>
          </section>

          {detail.approvals.length > 0 && (
            <section className="card card-pad space-y-3">
              <h2 className="text-sm font-semibold">待处理审批</h2>
              {detail.approvals.map((approval) => {
                const note = notes[approval.id] ?? "";
                const decidable = me ? canDecideWorkflowApproval(approval, run, me) : false;
                return (
                  <div key={approval.id} className="space-y-2 rounded-lg border border-line p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">{approval.title}</span>
                      <span className="badge bg-sage-100 text-sage-700">
                        {approvalSourceLabel(approval.source)}
                      </span>
                    </div>
                    <p className="text-xs text-ink-500">
                      节点 {approval.node_id || "—"}
                      {approval.tool_name && ` · 工具 ${approval.tool_name}`}
                      {approval.risk && ` · 风险 ${approval.risk}`}
                    </p>
                    {Object.keys(approval.payload).length > 0 && (
                      <pre className="max-h-32 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(approval.payload)}
                      </pre>
                    )}
                    {decidable ? (
                      <div className="space-y-1">
                        <textarea
                          className="input text-xs"
                          rows={2}
                          placeholder="备注（驳回必填）"
                          value={note}
                          onChange={(event) =>
                            setNotes({ ...notes, [approval.id]: event.target.value })
                          }
                        />
                        <div className="flex gap-2">
                          <button
                            className="btn-primary"
                            disabled={decide.isPending}
                            type="button"
                            onClick={() =>
                              decide.mutate({ approval, decision: "approve", note })
                            }
                          >
                            通过
                          </button>
                          <button
                            className="btn-danger"
                            disabled={decide.isPending || !note.trim()}
                            type="button"
                            onClick={() =>
                              decide.mutate({ approval, decision: "reject", note })
                            }
                          >
                            驳回
                          </button>
                        </div>
                      </div>
                    ) : (
                      <p className="text-xs text-ink-500">
                        由发起人之外的管理员处理。
                      </p>
                    )}
                  </div>
                );
              })}
            </section>
          )}

          <section className="card card-pad space-y-2">
            <h2 className="text-sm font-semibold">节点尝试</h2>
            {byNode.size === 0 && <p className="text-xs text-ink-500">还没有节点记录。</p>}
            {[...byNode.entries()].map(([nodeId, attempts]) => (
              <details key={nodeId} className="rounded-lg border border-line p-2">
                <summary className="cursor-pointer text-sm">
                  <span className="font-medium">{nodeId}</span>
                  <span className="ml-2 text-xs text-ink-500">
                    {attempts.length} 次 · {nodeKindLabel(attempts[0].kind)} ·{" "}
                    {nodeStatusLabel(attempts[attempts.length - 1].status)}
                  </span>
                </summary>
                <div className="mt-2 space-y-2">
                  {attempts.map((attempt) => (
                    <div key={`${attempt.node_id}-${attempt.attempt}`} className="space-y-1">
                      <p className="text-xs text-ink-500">
                        第 {attempt.attempt} 次 · {nodeStatusLabel(attempt.status)} ·{" "}
                        {attempt.duration_ms}ms
                        {attempt.error && ` · ${attempt.error}`}
                      </p>
                      {attempt.kind === "team" && attempt.agent_run_id && (
                        <Link
                          className="text-xs text-sage-700"
                          to={`/assistant/teams/${attempt.agent_run_id}`}
                        >
                          查看团队运行
                        </Link>
                      )}
                      <pre className="max-h-32 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(attempt.input)}
                      </pre>
                      <pre className="max-h-40 overflow-auto rounded bg-paper p-2 text-[11px] text-ink-700">
                        {pretty(attempt.output)}
                      </pre>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </section>
        </aside>
      </div>
    </div>
  );
}
