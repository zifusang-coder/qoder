import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { capabilityFor, specFor } from "../platforms";
import { formatTime, toDatetimeLocal, toScheduledAt } from "../schedule";
import {
  actionFor,
  canCancel,
  canReject,
  canReschedule,
  contentKindLabel,
  errorText,
  jobStatusLabel,
  platformLabel,
  type Account,
  type Job,
  type JobActionKind,
  type JobList,
  type PrecheckResult,
} from "../types";

type JobAction = { job: Job; action: JobActionKind; note?: string };

type ActionResult = {
  action: JobActionKind;
  passed?: boolean;
  status?: string;
};

type Feedback = { jobId: string; kind: "ok" | "warn" | "error"; text: string };

function successText(result: ActionResult): string {
  switch (result.action) {
    case "precheck":
      return result.passed
        ? "检查通过，已进入人工审核"
        : "检查未通过，请修改内容后重试";
    case "approve":
      return "已通过人工审核";
    case "execute":
      return "已提交到平台，请在平台后台确认";
    case "export":
      return "已导出素材包";
    case "reject":
      return "已驳回，任务回到草稿";
    case "cancel":
      return "已取消任务";
    case "retry":
      return "已重置为待执行，请重新执行发布";
    case "retry_export":
      return result.status === "exported"
        ? "已重新导出素材包"
        : "已重新提交导出";
  }
}

function issueLevelLabel(level: string): string {
  if (level === "error") return "错误";
  if (level === "warn") return "警告";
  return level;
}

export default function Publish() {
  const qc = useQueryClient();
  const { canWrite, isAdmin } = useAuth();
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [scheduleOpen, setScheduleOpen] = useState("");
  const [scheduleDraft, setScheduleDraft] = useState("");
  const [cancelingId, setCancelingId] = useState("");

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<JobList>("/publish/jobs"),
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });
  const accountNames = new Map(
    (accounts.data ?? []).map((account) => [account.id, account.name]),
  );

  const act = useMutation({
    mutationFn: async ({
      job,
      action,
      note,
    }: JobAction): Promise<ActionResult> => {
      if (action === "precheck") {
        const result = await api<PrecheckResult>(
          `/publish/jobs/${job.id}/precheck`,
          { method: "POST" },
        );
        return { action, passed: result.passed };
      }
      if (action === "retry_export") {
        await api<Job>(`/publish/jobs/${job.id}/actions`, {
          method: "POST",
          body: JSON.stringify({ action: "retry", note: note ?? "" }),
        });
        const exported = await api<Job>(`/publish/jobs/${job.id}/export`, {
          method: "POST",
        });
        return { action, status: exported.status };
      }
      if (
        action === "approve" ||
        action === "reject" ||
        action === "cancel" ||
        action === "retry"
      ) {
        const updated = await api<Job>(`/publish/jobs/${job.id}/actions`, {
          method: "POST",
          body: JSON.stringify({ action, note: note ?? "" }),
        });
        return { action, status: updated.status };
      }
      const updated = await api<Job>(`/publish/jobs/${job.id}/${action}`, {
        method: "POST",
      });
      return { action, status: updated.status };
    },
    onSuccess: (result, variables) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      setCancelingId("");
      setFeedback({
        jobId: variables.job.id,
        kind: result.action === "precheck" && !result.passed ? "warn" : "ok",
        text: successText(result),
      });
    },
    onError: (err, variables) => {
      setCancelingId("");
      setFeedback({
        jobId: variables.job.id,
        kind: "error",
        text: errorText(err),
      });
    },
  });

  const reschedule = useMutation({
    mutationFn: ({
      job,
      scheduledAt,
    }: {
      job: Job;
      scheduledAt: string | null;
    }) =>
      api<Job>(`/publish/jobs/${job.id}/schedule`, {
        method: "PATCH",
        body: JSON.stringify({ scheduled_at: scheduledAt }),
      }),
    onSuccess: (job, variables) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      setScheduleOpen("");
      setFeedback({
        jobId: variables.job.id,
        kind: "ok",
        text: variables.scheduledAt
          ? `已设置定时：${formatTime(job.scheduled_at)}`
          : "已清除定时",
      });
    },
    onError: (err, variables) => {
      setFeedback({
        jobId: variables.job.id,
        kind: "error",
        text: errorText(err),
      });
    },
  });

  function toggleSchedule(job: Job) {
    if (scheduleOpen === job.id) {
      setScheduleOpen("");
      return;
    }
    setScheduleOpen(job.id);
    setScheduleDraft(toDatetimeLocal(job.scheduled_at));
    setFeedback(null);
  }

  function saveSchedule(job: Job) {
    const scheduledAt = toScheduledAt(scheduleDraft);
    if (!scheduledAt) {
      setFeedback({
        jobId: job.id,
        kind: "error",
        text: "请选择有效的定时时间",
      });
      return;
    }
    setFeedback(null);
    reschedule.mutate({ job, scheduledAt });
  }

  function cancelJob(job: Job) {
    setFeedback(null);
    setCancelingId("");
    act.mutate({ job, action: "cancel" });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">发布</h1>
          <p className="mt-1 text-sm text-ink-500">
            审阅发布任务：公众号自动建草稿，其他平台导出素材包。
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs text-ink-500">
          {canWrite && (
            <Link className="text-sage-700" to="/accounts">
              管理发布账号
            </Link>
          )}
          <span>共 {jobs.data?.total ?? 0} 个任务</span>
        </div>
      </div>

      {jobs.isLoading && <p className="text-sm text-ink-500">加载中…</p>}
      {jobs.isError && (
        <p className="text-sm text-wine-600">{errorText(jobs.error)}</p>
      )}
      {jobs.isSuccess && jobs.data.items.length === 0 && (
        <p className="card p-6 text-sm text-ink-500">
          还没有发布任务，请先在项目详情中创建。
        </p>
      )}

      <div className="space-y-3">
        {jobs.data?.items.map((job) => {
          const spec = specFor(job.platform);
          const capability = spec
            ? capabilityFor(spec, job.content_kind)
            : "pending";
          const action = actionFor(job);
          const actionNeedsAdmin =
            action?.action === "approve" ||
            action?.action === "execute" ||
            action?.action === "retry" ||
            action?.action === "retry_export";
          const canAct =
            action !== null && (actionNeedsAdmin ? isAdmin : canWrite);
          const pending = act.isPending && act.variables?.job.id === job.id;
          const schedulePending =
            reschedule.isPending && reschedule.variables?.job.id === job.id;
          const note = feedback?.jobId === job.id ? feedback : null;
          const missingAccount =
            capability === "auto_publish" && !job.account_id;
          const accountLabel = job.account_id
            ? (accountNames.get(job.account_id) ?? job.account_id)
            : "";
          return (
            <article
              key={job.id}
              className="card p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-medium">
                    {platformLabel(job.platform)} ·{" "}
                    {contentKindLabel(job.content_kind)}
                    <span className="ml-2 text-sm font-normal text-ink-500">
                      v{job.content_version}
                    </span>
                  </h2>
                  <p className="mt-1 text-xs text-ink-500">
                    状态：{jobStatusLabel(job.status)} · 创建于{" "}
                    {formatTime(job.created_at)}
                    {job.scheduled_at &&
                      ` · 定时：${formatTime(job.scheduled_at)}`}
                  </p>
                  {capability === "auto_publish" && (
                    <p className="mt-1 text-xs text-ink-500">
                      {job.account_id ? (
                        `发布账号：${accountLabel}`
                      ) : (
                        <>
                          未关联发布账号，
                          <Link className="text-sage-700" to="/accounts">
                            去授权
                          </Link>
                        </>
                      )}
                    </p>
                  )}
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {canReject(job) && isAdmin && (
                    <button
                      type="button"
                      className="rounded border border-wine-100 px-4 py-2 text-sm text-warm-600 hover:border-warm-600 disabled:opacity-50"
                      disabled={act.isPending}
                      onClick={() => {
                        setFeedback(null);
                        act.mutate({
                          job,
                          action: "reject",
                          note: notes[job.id] ?? "",
                        });
                      }}
                    >
                      驳回
                    </button>
                  )}
                  {canCancel(job) &&
                    isAdmin &&
                    (cancelingId === job.id ? (
                      <span className="flex flex-wrap items-center gap-2 rounded border border-wine-100 px-3 py-1.5 text-sm text-wine-600">
                        取消后需重新创建任务
                        <button
                          type="button"
                          className="rounded bg-wine-600 px-2 py-1 text-xs font-medium text-white hover:bg-wine-700 disabled:opacity-50"
                          disabled={act.isPending}
                          onClick={() => cancelJob(job)}
                        >
                          {act.isPending ? "取消中…" : "确认取消"}
                        </button>
                        <button
                          type="button"
                          className="rounded px-2 py-1 text-xs text-ink-500 hover:text-ink-900"
                          onClick={() => setCancelingId("")}
                        >
                          返回
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="rounded border border-wine-100 px-4 py-2 text-sm text-wine-600 hover:border-wine-100 disabled:opacity-50"
                        disabled={act.isPending}
                        onClick={() => {
                          setFeedback(null);
                          setCancelingId(job.id);
                        }}
                      >
                        取消任务
                      </button>
                    ))}
                  {canReschedule(job) && isAdmin && (
                    <button
                      type="button"
                      className="rounded border border-line px-4 py-2 text-sm text-ink-700 hover:border-ink-300 disabled:opacity-50"
                      disabled={schedulePending}
                      onClick={() => toggleSchedule(job)}
                    >
                      {scheduleOpen === job.id ? "收起定时" : "定时"}
                    </button>
                  )}
                  {canAct && action ? (
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={
                        act.isPending ||
                        (action.action === "execute" && missingAccount)
                      }
                      title={
                        action.action === "execute" && missingAccount
                          ? "请先为任务关联公众号账号"
                          : undefined
                      }
                      onClick={() => {
                        setFeedback(null);
                        act.mutate({
                          job,
                          action: action.action,
                          note: notes[job.id] ?? "",
                        });
                      }}
                    >
                      {pending ? "处理中…" : action.label}
                    </button>
                  ) : job.status === "approved" && action === null ? (
                    capability === "pending" ? (
                      <span className="text-sm text-ink-500">
                        {spec?.note ?? "该平台暂不支持自动发布或导出"}
                      </span>
                    ) : null
                  ) : null}
                </div>
              </div>

              {missingAccount && job.status === "approved" && (
                <p className="mt-3 text-sm text-warm-600">
                  该任务创建时未关联账号，请取消后在项目详情重新创建并选择账号。
                </p>
              )}

              {canReject(job) && isAdmin && (
                <input
                  className="mt-3 input text-sm text-ink-900"
                  maxLength={500}
                  value={notes[job.id] ?? ""}
                  onChange={(e) =>
                    setNotes((prev) => ({ ...prev, [job.id]: e.target.value }))
                  }
                  placeholder="驳回原因（可选），会写入发布事件备注"
                />
              )}

              {scheduleOpen === job.id && isAdmin && (
                <div className="mt-3 space-y-2 rounded border border-line bg-white p-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="min-w-56 flex-1 space-y-1 text-sm text-ink-500">
                      <span>定时发布时间</span>
                      <input
                        className="input text-ink-900"
                        type="datetime-local"
                        value={scheduleDraft}
                        onChange={(e) => setScheduleDraft(e.target.value)}
                      />
                    </label>
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={schedulePending || !scheduleDraft}
                      onClick={() => saveSchedule(job)}
                    >
                      {schedulePending ? "保存中…" : "保存"}
                    </button>
                    <button
                      type="button"
                      className="rounded border border-line px-4 py-2 text-sm text-ink-700 disabled:opacity-50"
                      disabled={schedulePending || !job.scheduled_at}
                      onClick={() => {
                        setFeedback(null);
                        reschedule.mutate({ job, scheduledAt: null });
                      }}
                    >
                      清除定时
                    </button>
                  </div>
                  <p className="text-xs text-ink-500">
                    按本机时区解释，提交时转换为 UTC；worker 到期后执行已审核的自动发布任务。
                  </p>
                </div>
              )}

              {job.checks.passed !== undefined && (
                <p
                  className={`mt-3 text-sm ${
                    job.checks.passed ? "text-sage-600" : "text-wine-600"
                  }`}
                >
                  {job.checks.passed ? "发布前检查通过" : "发布前检查未通过"}
                </p>
              )}
              {job.checks.issues && job.checks.issues.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {job.checks.issues.map((issue, index) => (
                    <li
                      key={`${issue.level}-${index}`}
                      className={
                        issue.level === "error"
                          ? "text-sm text-wine-600"
                          : "text-sm text-warm-600"
                      }
                    >
                      [{issueLevelLabel(issue.level)}] {issue.message}
                    </li>
                  ))}
                </ul>
              )}

              {job.error &&
                (job.status === "publish_failed" ||
                  job.status === "export_failed") && (
                  <p className="mt-3 whitespace-pre-wrap text-sm text-wine-600">
                    {job.error}
                  </p>
                )}

              {(job.external_id || job.external_url || job.export_path) && (
                <div className="mt-3 space-y-1 text-xs text-ink-500">
                  {job.external_id && (
                    <p>
                      外部 ID：
                      {job.external_url ? (
                        <a
                          className="text-sage-700"
                          href={job.external_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {job.external_id}
                        </a>
                      ) : (
                        <span className="font-mono">{job.external_id}</span>
                      )}
                    </p>
                  )}
                  {job.export_path && (
                    <p>
                      素材包：
                      <span className="font-mono">{job.export_path}</span>
                    </p>
                  )}
                </div>
              )}

              {note && (
                <p
                  className={`mt-3 text-sm ${
                    note.kind === "ok"
                      ? "text-sage-600"
                      : note.kind === "warn"
                        ? "text-warm-600"
                        : "text-wine-600"
                  }`}
                >
                  {note.text}
                </p>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
