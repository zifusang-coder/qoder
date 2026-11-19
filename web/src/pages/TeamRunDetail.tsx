import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  agentStatusLabel,
  artifactLink,
  isRunTerminal,
  usageTokens,
  type AgentRun,
} from "../agent";
import { canCancelTeamRun, canDecideTeamChildApproval, entryKindLabel, roleLabel, teamTotals, type TeamRunDetail } from "../agentTeam";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";
import { runStatusBadge } from "../workflows";

export default function TeamRunDetailPage() {
  const { id } = useParams();
  const { user: me } = useAuth();
  const queryClient = useQueryClient();
  const [cancelError, setCancelError] = useState("");
  const [note, setNote] = useState("");
  const [decideError, setDecideError] = useState("");
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
  const decide = useMutation({
    mutationFn: (input: { id: string; decision: "approve" | "reject" }) =>
      api<AgentRun>(`/agent/approvals/${input.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision: input.decision, note }),
      }),
    onSuccess: () => {
      setNote("");
      setDecideError("");
      void queryClient.invalidateQueries({ queryKey: ["team-run", id] });
      void queryClient.invalidateQueries({ queryKey: ["agent-runs"] });
    },
    onError: (err) => setDecideError(errorText(err)),
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
        {me && canCancelTeamRun(run, me) && (
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

      {children.some((child) => child.pending_approval) && (
        <section className="card card-pad space-y-3">
          <h2 className="text-sm font-semibold">待审批</h2>
          {children.map((child) => {
            const approval = child.pending_approval;
            if (!approval) return null;
            const canDecide = me
              ? canDecideTeamChildApproval(child, run.user_id, me)
              : false;
            return (
              <div key={child.id} className="space-y-2 rounded-lg border border-wine-100 bg-wine-50 p-3">
                <div className="flex items-center gap-2 text-sm">
                  <span className="badge bg-line text-ink-500">团队 · {roleLabel(child.role)}</span>
                  <span className="font-medium">待确认：{approval.tool_name}</span>
                </div>
                <pre className="max-h-48 overflow-auto rounded bg-paper p-3 text-xs">
                  {JSON.stringify(approval.payload, null, 2)}
                </pre>
                {canDecide ? (
                  <>
                    <input
                      className="input w-full"
                      placeholder="审批备注（通过可选，驳回必填）"
                      value={note}
                      maxLength={2000}
                      onChange={(event) => setNote(event.target.value)}
                    />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="btn-primary"
                        disabled={decide.isPending}
                        onClick={() => decide.mutate({ id: approval.id, decision: "approve" })}
                      >
                        通过
                      </button>
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={decide.isPending || note.trim().length === 0}
                        onClick={() => decide.mutate({ id: approval.id, decision: "reject" })}
                      >
                        驳回
                      </button>
                    </div>
                  </>
                ) : (
                  <p className="text-xs text-ink-500">等待其他管理员确认；发起人不能审批自己的任务。</p>
                )}
              </div>
            );
          })}
          {decideError && <p className="text-sm text-wine-700">{decideError}</p>}
        </section>
      )}

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
