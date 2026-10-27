import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../client";
import {
  errorText,
  isManualExportPlatform,
  platformLabel,
  type Job,
  type JobList,
  type PrecheckResult,
} from "../types";

type JobActionKind = "precheck" | "approve" | "execute" | "export";

type JobAction = { job: Job; action: JobActionKind };

type ActionResult = { action: JobActionKind; passed?: boolean };

type Feedback = { jobId: string; kind: "ok" | "warn" | "error"; text: string };

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  prechecking: "检查中",
  precheck_failed: "检查未通过",
  awaiting_review: "待人工审核",
  approved: "已通过审核",
  exporting: "导出中",
  exported: "已导出",
  export_failed: "导出失败",
  publishing: "发布中",
  published: "已发布",
  publish_failed: "发布失败",
  cancelled: "已取消",
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function actionFor(job: Job): { action: JobActionKind; label: string } | null {
  if (job.status === "draft" || job.status === "precheck_failed") {
    return { action: "precheck", label: "发布前检查" };
  }
  if (job.status === "awaiting_review") {
    return { action: "approve", label: "人工通过" };
  }
  if (job.status === "approved") {
    if (job.platform === "wechat") {
      return { action: "execute", label: "发布到公众号草稿" };
    }
    if (isManualExportPlatform(job.platform)) {
      return { action: "export", label: "导出素材包" };
    }
  }
  return null;
}

function successText(result: ActionResult): string {
  if (result.action === "precheck") {
    return result.passed
      ? "检查通过，已进入人工审核"
      : "检查未通过，请修改内容后重试";
  }
  if (result.action === "approve") return "已通过人工审核";
  if (result.action === "execute") return "已提交到公众号草稿箱";
  return "已导出素材包";
}

export default function Publish() {
  const qc = useQueryClient();
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<JobList>("/publish/jobs"),
  });

  const act = useMutation({
    mutationFn: async ({ job, action }: JobAction): Promise<ActionResult> => {
      if (action === "precheck") {
        const result = await api<PrecheckResult>(
          `/publish/jobs/${job.id}/precheck`,
          { method: "POST" },
        );
        return { action, passed: result.passed };
      }
      if (action === "approve") {
        await api<Job>(`/publish/jobs/${job.id}/actions`, {
          method: "POST",
          body: JSON.stringify({ action: "approve" }),
        });
        return { action };
      }
      await api<Job>(`/publish/jobs/${job.id}/${action}`, { method: "POST" });
      return { action };
    },
    onSuccess: (result, variables) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      setFeedback({
        jobId: variables.job.id,
        kind: result.action === "precheck" && !result.passed ? "warn" : "ok",
        text: successText(result),
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

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">发布</h1>
          <p className="mt-1 text-sm text-slate-400">
            审阅发布任务：公众号自动建草稿，其他平台导出素材包。
          </p>
        </div>
        <span className="text-xs text-slate-500">
          共 {jobs.data?.total ?? 0} 个任务
        </span>
      </div>

      {jobs.isLoading && <p className="text-sm text-slate-400">加载中…</p>}
      {jobs.isError && (
        <p className="text-sm text-red-400">{errorText(jobs.error)}</p>
      )}
      {jobs.isSuccess && jobs.data.items.length === 0 && (
        <p className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
          还没有发布任务，请先在项目详情中创建。
        </p>
      )}

      <div className="space-y-3">
        {jobs.data?.items.map((job) => {
          const action = actionFor(job);
          const pending = act.isPending && act.variables?.job.id === job.id;
          const note = feedback?.jobId === job.id ? feedback : null;
          return (
            <article
              key={job.id}
              className="rounded-xl border border-slate-800 bg-slate-900 p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-medium">
                    {platformLabel(job.platform)}
                    <span className="ml-2 text-sm font-normal text-slate-400">
                      v{job.content_version}
                    </span>
                  </h2>
                  <p className="mt-1 text-xs text-slate-500">
                    状态：{statusLabel(job.status)} · 创建于{" "}
                    {formatTime(job.created_at)}
                  </p>
                </div>
                {action ? (
                  <button
                    type="button"
                    className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
                    disabled={act.isPending}
                    onClick={() => {
                      setFeedback(null);
                      act.mutate({ job, action: action.action });
                    }}
                  >
                    {pending ? "处理中…" : action.label}
                  </button>
                ) : job.status === "approved" ? (
                  <span className="text-sm text-slate-500">
                    {platformLabel(job.platform)}暂不支持自动发布或导出
                  </span>
                ) : null}
              </div>

              {job.checks.passed !== undefined && (
                <p
                  className={`mt-3 text-sm ${
                    job.checks.passed ? "text-emerald-400" : "text-red-400"
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
                          ? "text-sm text-red-400"
                          : "text-sm text-amber-400"
                      }
                    >
                      [{issue.level}] {issue.message}
                    </li>
                  ))}
                </ul>
              )}

              {job.error && (
                <p className="mt-3 whitespace-pre-wrap text-sm text-red-400">
                  {job.error}
                </p>
              )}

              {(job.external_id || job.external_url || job.export_path) && (
                <div className="mt-3 space-y-1 text-xs text-slate-500">
                  {job.external_id && (
                    <p>
                      外部 ID：
                      {job.external_url ? (
                        <a
                          className="text-indigo-400"
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
                      素材包：<span className="font-mono">{job.export_path}</span>
                    </p>
                  )}
                </div>
              )}

              {note && (
                <p
                  className={`mt-3 text-sm ${
                    note.kind === "ok"
                      ? "text-emerald-400"
                      : note.kind === "warn"
                        ? "text-amber-400"
                        : "text-red-400"
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

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
