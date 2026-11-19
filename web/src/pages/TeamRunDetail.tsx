import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  agentStatusLabel,
  artifactLink,
  canCancelRun,
  isRunTerminal,
  usageTokens,
  type AgentRun,
} from "../agent";
import { entryKindLabel, roleLabel, teamTotals, type TeamRunDetail } from "../agentTeam";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";
import { runStatusBadge } from "../workflows";

export default function TeamRunDetailPage() {
  const { id } = useParams();
  const { user: me } = useAuth();
  const queryClient = useQueryClient();
  const [cancelError, setCancelError] = useState("");
  const detail = useQuery({
    queryKey: ["team-run", id],
    queryFn: () => api<TeamRunDetail>(`/agent/team/runs/${id}`),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 2000;
      return isRunTerminal(data.run.status) ? false : 2000;
    },
  });
  const cancelRun = useMutation({
    mutationFn: () => api<AgentRun>(`/agent/team/runs/${id}/cancel`, { method: "POST" }),
    onSuccess: () => {
      setCancelError("");
      void queryClient.invalidateQueries({ queryKey: ["team-run", id] });
    },
    onError: (err) => setCancelError(errorText(err)),
  });
  if (detail.isLoading) return <p className="text-sm text-ink-500">加载中…</p>;
  if (detail.isError || !detail.data) return <p className="text-sm text-wine-700">加载失败</p>;
  const { run, children, entries, artifacts } = detail.data;
  const totals = teamTotals(children);
  return (
    <div className="space-y-4">
      <header className="card card-pad space-y-2">
        <div className="flex items-center gap-2">
          <h1 className="text-lg font-semibold">协作团队</h1>
          <span className={runStatusBadge(run.status)}>{agentStatusLabel(run.status)}</span>
        </div>
        <p className="text-sm text-ink-700 whitespace-pre-wrap">{run.goal}</p>
        <p className="text-xs text-ink-500">
          子运行 token {totals.tokens} · 工具调用 {totals.tool_calls} ·
          Lead token {usageTokens(run.usage ?? {})}
        </p>
        {run.error_code && (
          <p className="text-sm text-wine-700">{run.error_code}：{run.error_message}</p>
        )}
        {me && canCancelRun(run, me) && (
          <div>
            <button
              type="button"
              className="btn-secondary"
              disabled={cancelRun.isPending}
              onClick={() => cancelRun.mutate()}
            >
              取消任务
            </button>
          </div>
        )}
        {cancelError && <p className="text-sm text-wine-700">{cancelError}</p>}
      </header>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">角色时间线</h2>
        {children.length === 0 && <p className="text-xs text-ink-500">暂无角色执行</p>}
        {children.map((child) => (
          <div key={child.id} className="rounded-lg border border-line p-3 space-y-1">
            <div className="flex items-center gap-2 text-sm">
              <span className="font-medium">{roleLabel(child.role)}</span>
              <span className={runStatusBadge(child.status)}>{agentStatusLabel(child.status)}</span>
              <span className="text-xs text-ink-500">尝试 {child.attempts}</span>
            </div>
            {child.result_text && <p className="text-xs text-ink-700">{child.result_text}</p>}
            {child.error_code && <p className="text-xs text-wine-700">{child.error_code}</p>}
          </div>
        ))}
      </section>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">黑板</h2>
        {entries.map((entry) => (
          <div key={entry.seq} className="rounded-lg border border-line p-3 space-y-1">
            <div className="flex items-center gap-2 text-xs text-ink-500">
              <span>#{entry.seq}</span>
              <span>{entryKindLabel(entry.kind)}</span>
              {entry.role && <span>{roleLabel(entry.role)}</span>}
            </div>
            <pre className="text-xs text-ink-700 whitespace-pre-wrap break-all">
              {JSON.stringify(entry.content, null, 2)}
            </pre>
          </div>
        ))}
      </section>

      <section className="card card-pad space-y-2">
        <h2 className="text-sm font-semibold">产物</h2>
        {artifacts.length === 0 && <p className="text-xs text-ink-500">暂无产物</p>}
        {artifacts.map((artifact, index) => (
          <Link
            key={index}
            to={artifactLink({ type: artifact.kind, id: artifact.id, title: artifact.title })}
            className="block text-sm text-sage-700 underline"
          >
            {artifact.title || artifact.id}
          </Link>
        ))}
      </section>
    </div>
  );
}