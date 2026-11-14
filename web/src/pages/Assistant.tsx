import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  agentStatusLabel,
  artifactLabel,
  artifactLink,
  budgetPercent,
  canCancelRun,
  canCreateRun,
  canDecideApproval,
  canRetryRun,
  firstLine,
  formatElapsed,
  groupSteps,
  isRunTerminal,
  stepKindLabel,
  stepStatusLabel,
  usageTokens,
  type AgentRun,
  type AgentRunDetail,
  type Artifact,
} from "../agent";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";

const BUDGET_LABELS: Record<string, string> = {
  max_steps: "步数",
  max_tokens: "Token",
  max_tool_calls: "工具调用",
  max_seconds: "时长",
};

function usageValue(run: AgentRun, key: string): number {
  const usage = run.usage ?? {};
  if (key === "max_tokens") return usageTokens(usage);
  if (key === "max_steps") return Number(usage.steps ?? 0) || 0;
  if (key === "max_tool_calls") return Number(usage.tool_calls ?? 0) || 0;
  if (key === "max_seconds") {
    return Math.round((Number(usage.elapsed_ms ?? 0) || 0) / 1000);
  }
  return 0;
}

export default function Assistant() {
  const { user: me } = useAuth();
  const queryClient = useQueryClient();
  const [goal, setGoal] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const runs = useQuery({
    queryKey: ["agent-runs"],
    queryFn: () => api<AgentRun[]>("/agent/runs?limit=50"),
    refetchInterval: 3000,
  });

  const activeId = selected ?? runs.data?.[0]?.id ?? null;
  const detail = useQuery({
    queryKey: ["agent-run", activeId],
    queryFn: () => api<AgentRunDetail>(`/agent/runs/${activeId}`),
    enabled: Boolean(activeId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return !status || isRunTerminal(status) ? false : 2000;
    },
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["agent-runs"] });
    void queryClient.invalidateQueries({ queryKey: ["agent-run", activeId] });
  };

  const createRun = useMutation({
    mutationFn: () =>
      api<AgentRun>("/agent/runs", {
        method: "POST",
        body: JSON.stringify({ goal }),
      }),
    onSuccess: (run) => {
      setGoal("");
      setError("");
      setSelected(run.id);
      refresh();
    },
    onError: (err) => setError(errorText(err)),
  });

  const cancelRun = useMutation({
    mutationFn: (runId: string) =>
      api<AgentRun>(`/agent/runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: refresh,
    onError: (err) => setError(errorText(err)),
  });

  const retryRun = useMutation({
    mutationFn: (runId: string) =>
      api<AgentRun>(`/agent/runs/${runId}/retry`, { method: "POST" }),
    onSuccess: (run) => {
      setSelected(run.id);
      refresh();
    },
    onError: (err) => setError(errorText(err)),
  });

  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject" }) =>
      api<AgentRun>(`/agent/approvals/${input.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision: input.decision, note }),
      }),
    onSuccess: () => {
      setNote("");
      refresh();
    },
    onError: (err) => setError(errorText(err)),
  });

  const run = detail.data ?? null;
  const pending = run?.pending_approval ?? null;
  const artifacts = Array.isArray(run?.result?.artifacts)
    ? (run.result.artifacts as Artifact[])
    : [];
  const canDecide = run && me ? canDecideApproval(run, me) : false;

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">AI 助理</h1>
        <p className="text-sm text-ink-500">
          描述目标，助理会规划步骤并调用工具；发布前仍需你确认。
        </p>
      </header>

      {me && canCreateRun(me.role) && (
        <section className="card space-y-3 p-4">
          <textarea
            className="input min-h-24 w-full"
            placeholder="例如：为「秋季护肤」项目写一版小红书文案并准备发布草稿"
            value={goal}
            maxLength={4000}
            onChange={(event) => setGoal(event.target.value)}
          />
          <div className="flex items-center justify-between">
            <span className="text-xs text-ink-500">{goal.length}/4000</span>
            <button
              type="button"
              className="btn-primary"
              disabled={!goal.trim() || createRun.isPending}
              onClick={() => createRun.mutate()}
            >
              {createRun.isPending ? "提交中…" : "开始"}
            </button>
          </div>
        </section>
      )}

      {error && <p className="text-sm text-wine-600">{error}</p>}

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <aside className="card divide-y divide-line">
          {runs.isLoading && <p className="p-4 text-sm text-ink-500">加载中…</p>}
          {runs.data?.length === 0 && (
            <p className="p-4 text-sm text-ink-500">还没有任务。</p>
          )}
          {runs.data?.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setSelected(item.id)}
              className={`block w-full p-3 text-left ${
                item.id === activeId ? "bg-sage-50" : ""
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="badge bg-sage-100 text-sage-700">
                  {agentStatusLabel(item.status)}
                </span>
                <span className="text-xs text-ink-500">
                  {formatElapsed(item.started_at, item.finished_at)}
                </span>
              </div>
              <p className="mt-1 line-clamp-2 text-sm">{firstLine(item.goal)}</p>
              {item.pending_approval && (
                <p className="mt-1 text-xs text-wine-600">等待审批</p>
              )}
            </button>
          ))}
        </aside>

        <section className="space-y-4">
          {!run && <p className="text-sm text-ink-500">选择或创建一个任务。</p>}
          {run && (
            <>
              <div className="card space-y-3 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="text-lg font-medium">{firstLine(run.goal, 80)}</h2>
                  <span className="badge bg-sage-100 text-sage-700">
                    {agentStatusLabel(run.status)}
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-sm text-ink-700">
                  {run.goal}
                </p>
                {run.error_code && (
                  <p className="text-sm text-wine-600">
                    {run.error_message || run.error_code}
                  </p>
                )}
                <div className="flex flex-wrap gap-2">
                  {me && canCancelRun(run, me) && (
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={cancelRun.isPending}
                      onClick={() => cancelRun.mutate(run.id)}
                    >
                      取消任务
                    </button>
                  )}
                  {me && canRetryRun(run.status, me.role) && (
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={retryRun.isPending}
                      onClick={() => retryRun.mutate(run.id)}
                    >
                      重新运行
                    </button>
                  )}
                </div>
              </div>

              <div className="card space-y-3 p-4">
                <h3 className="text-sm font-medium text-ink-700">预算</h3>
                {Object.keys(run.budget).length === 0 && (
                  <p className="text-sm text-ink-500">使用服务端默认预算。</p>
                )}
                {Object.entries(run.budget).map(([key, max]) => {
                  const used = usageValue(run, key);
                  const percent = budgetPercent(used, Number(max));
                  return (
                    <div key={key} className="space-y-1">
                      <div className="flex justify-between text-xs text-ink-500">
                        <span>{BUDGET_LABELS[key] ?? key}</span>
                        <span>
                          {used} / {max}
                        </span>
                      </div>
                      <div className="h-2 rounded bg-line">
                        <div
                          className="h-2 rounded bg-sage-500"
                          style={{ width: `${percent}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>

              {pending && (
                <div className="card space-y-3 border border-wine-100 bg-wine-50 p-4">
                  <h3 className="text-sm font-medium">
                    待确认：{pending.tool_name}
                  </h3>
                  <pre className="max-h-48 overflow-auto rounded bg-paper p-3 text-xs">
                    {JSON.stringify(pending.payload, null, 2)}
                  </pre>
                  {canDecide ? (
                    <>
                      <input
                        className="input w-full"
                        placeholder="审批备注（可选）"
                        value={note}
                        maxLength={2000}
                        onChange={(event) => setNote(event.target.value)}
                      />
                      <div className="flex gap-2">
                        <button
                          type="button"
                          className="btn-primary"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ id: pending.id, decision: "approve" })
                          }
                        >
                          通过
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={decide.isPending}
                          onClick={() =>
                            decide.mutate({ id: pending.id, decision: "reject" })
                          }
                        >
                          驳回
                        </button>
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-ink-500">
                      等待其他管理员确认；发起人不能审批自己的任务。
                    </p>
                  )}
                </div>
              )}

              <div className="card space-y-4 p-4">
                <h3 className="text-sm font-medium text-ink-700">步骤</h3>
                {run.steps.length === 0 && (
                  <p className="text-sm text-ink-500">尚未产生步骤。</p>
                )}
                {groupSteps(run.steps).map((group, index) => (
                  <div
                    key={group.llm?.step_index ?? `lead-${index}`}
                    className="space-y-2 border-l-2 border-line pl-3"
                  >
                    {group.llm && (
                      <p className="text-xs text-ink-500">
                        {stepKindLabel(group.llm.kind)} #{group.llm.step_index}
                        {group.llm.status !== "ok" &&
                          ` · ${stepStatusLabel(group.llm.status)}`}
                      </p>
                    )}
                    {group.actions.map((step) => (
                      <div key={step.step_index} className="rounded bg-paper p-2">
                        <div className="flex items-center justify-between gap-2 text-sm">
                          <span>
                            {stepKindLabel(step.kind)}
                            {step.tool_name ? ` · ${step.tool_name}` : ""}
                          </span>
                          <span className="badge bg-sage-100 text-sage-700">
                            {stepStatusLabel(step.status)}
                          </span>
                        </div>
                        {step.error && (
                          <p className="mt-1 text-xs text-wine-600">{step.error}</p>
                        )}
                      </div>
                    ))}
                  </div>
                ))}
              </div>

              {run.result_text && (
                <div className="card space-y-2 p-4">
                  <h3 className="text-sm font-medium text-ink-700">结果</h3>
                  <p className="whitespace-pre-wrap text-sm">{run.result_text}</p>
                </div>
              )}

              {artifacts.length > 0 && (
                <div className="card space-y-2 p-4">
                  <h3 className="text-sm font-medium text-ink-700">产物</h3>
                  <ul className="space-y-2">
                    {artifacts.map((artifact, index) => (
                      <li key={`${artifact.type}-${artifact.id}-${index}`}>
                        <a
                          className="text-sm text-sage-700 underline"
                          href={artifactLink(artifact)}
                        >
                          {artifactLabel(artifact.type ?? "")}
                          {artifact.title ? `：${artifact.title}` : ""}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
