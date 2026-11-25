import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, useState } from "react";
import { Link } from "react-router-dom";
import {
  engagementPercent,
  rankTimeBuckets,
  sharePercent,
  slotLabel,
  type AnalyticsInsights,
} from "../analytics";
import { useAuth } from "../auth-context";
import { api } from "../client";
import EmptyState, { InlineError } from "../components/EmptyState";
import {
  errorText,
  platformLabel,
  type AnalyticsOverview,
  type ImportResult,
  type PostmortemResult,
  type ProjectList,
} from "../types";

const MAX_CSV_BYTES = 5 * 1024 * 1024;

type ImportNotice = { kind: "ok" | "warn" | "error"; text: string };
type PostmortemFeedback = {
  contentId: string;
  kind: "ok" | "error";
  text: string;
};

export default function Analytics() {
  const qc = useQueryClient();
  const { canWrite } = useAuth();
  const [projectId, setProjectId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [importNotice, setImportNotice] = useState<ImportNotice | null>(null);
  const [postmortemFeedback, setPostmortemFeedback] =
    useState<PostmortemFeedback | null>(null);
  const [deletingId, setDeletingId] = useState("");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<ProjectList>("/projects"),
  });

  const effectiveProjectId = projectId || projects.data?.items[0]?.id || "";

  const overview = useQuery({
    queryKey: ["analytics", "overview", effectiveProjectId],
    queryFn: () =>
      api<AnalyticsOverview>(
        `/analytics/overview?project_id=${encodeURIComponent(effectiveProjectId)}`,
      ),
    enabled: Boolean(effectiveProjectId),
  });

  const insights = useQuery({
    queryKey: ["analytics", "insights", effectiveProjectId],
    queryFn: () =>
      api<AnalyticsInsights>(
        `/analytics/insights?project_id=${encodeURIComponent(effectiveProjectId)}`,
      ),
    enabled: Boolean(effectiveProjectId),
  });

  const importCsv = useMutation({
    mutationFn: (selected: File) => {
      const formData = new FormData();
      formData.append("file", selected);
      return api<ImportResult>("/analytics/import", {
        method: "POST",
        body: formData,
      });
    },
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["analytics"] });
      setImportNotice({
        kind: result.errors.length > 0 ? "warn" : "ok",
        text: `导入成功 ${result.upserted} 行，失败 ${result.errors.length} 行`,
      });
    },
    onError: (err) =>
      setImportNotice({ kind: "error", text: errorText(err) }),
  });

  const postmortem = useMutation({
    mutationFn: (contentId: string) =>
      api<PostmortemResult>(`/analytics/contents/${contentId}/postmortem`, {
        method: "POST",
        body: JSON.stringify({}),
      }),
    onSuccess: (result, contentId) => {
      qc.invalidateQueries({ queryKey: ["memories"] });
      setPostmortemFeedback({
        contentId,
        kind: "ok",
        text: `复盘完成，已写入 ${result.memory_ids.length} 条画像记忆`,
      });
    },
    onError: (err, contentId) =>
      setPostmortemFeedback({
        contentId,
        kind: "error",
        text: errorText(err),
      }),
  });

  const remove = useMutation({
    mutationFn: (contentId: string) =>
      api<void>(`/contents/${contentId}`, { method: "DELETE" }),
    onSuccess: () => {
      setDeletingId("");
      setPostmortemFeedback(null);
      qc.invalidateQueries({ queryKey: ["analytics"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err, contentId) =>
      setPostmortemFeedback({
        contentId,
        kind: "error",
        text: errorText(err),
      }),
  });

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    // Capture the selection, then clear the input so picking the same file
    // again (e.g. after a failed import) still fires onChange.
    const selected = event.target.files?.[0] ?? null;
    event.target.value = "";
    importCsv.reset();
    setImportNotice(null);
    setFile(selected);
  }

  function submitImport() {
    if (!file) {
      setImportNotice({ kind: "error", text: "请选择 CSV 文件" });
      return;
    }
    if (file.size > MAX_CSV_BYTES) {
      setImportNotice({ kind: "error", text: "CSV 不能超过 5MB" });
      return;
    }
    setImportNotice(null);
    importCsv.mutate(file);
  }

  const totals = overview.data
    ? [
        { label: "阅读", value: overview.data.totals.views },
        { label: "点赞", value: overview.data.totals.likes },
        { label: "评论", value: overview.data.totals.comments },
        { label: "转发", value: overview.data.totals.shares },
        { label: "收藏", value: overview.data.totals.saves },
        { label: "涨粉", value: overview.data.totals.followers_delta },
      ]
    : [];

  const insightData = insights.data;
  const channels = insightData?.channels ?? [];
  const rankedContents = insightData?.contents ?? [];
  const bestSlots = insightData?.best_slots ?? [];
  const timeBuckets = insightData ? rankTimeBuckets(insightData.time_buckets) : [];
  const topChannelViews = channels[0]?.views ?? 0;
  const hasTimeData = insightData?.has_time_data ?? false;
  const publishedContents = insightData?.published_contents ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">数据</h1>
        <p className="mt-1 text-sm text-ink-500">
          导入发布数据，按渠道、内容和时段查看表现，并生成单篇复盘。
        </p>
      </div>

      <div className="grid gap-4 card p-5 md:grid-cols-2">
        <label className="min-w-0 space-y-1 text-sm text-ink-500">
          <span>项目</span>
          <select
            className="input text-ink-900"
            value={effectiveProjectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={!projects.data || projects.data.items.length === 0}
          >
            {(projects.data?.items ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.title}
              </option>
            ))}
          </select>
        </label>
        <div className="min-w-0 space-y-1 text-sm text-ink-500">
          <span>导入 CSV</span>
          <div className="flex min-w-0 items-center gap-2">
            <input
              className="min-w-0 flex-1 text-sm text-ink-700 file:mr-3 file:rounded file:border-0 file:bg-brand-50 file:px-3 file:py-1.5 file:text-ink-700"
              type="file"
              accept=".csv"
              aria-label="导入 CSV 文件"
              disabled={!canWrite}
              onChange={chooseFile}
            />
            <button
              type="button"
              className="btn-primary"
              disabled={!canWrite || !file || importCsv.isPending}
              onClick={submitImport}
            >
              {importCsv.isPending ? "导入中…" : "导入"}
            </button>
          </div>
          {file && (
            <p className="truncate text-xs text-ink-500">已选择：{file.name}</p>
          )}
        </div>
      </div>

      {projects.isError && (
        <InlineError>{errorText(projects.error)}</InlineError>
      )}
      {projects.isSuccess && projects.data.items.length === 0 && (
        <EmptyState
          title="还没有项目"
          hint="先创建项目并导入发布数据，分析页才会有内容。"
          action={
            <Link className="text-brand-700 hover:underline" to="/projects">
              创建项目
            </Link>
          }
        />
      )}

      {importNotice && (
        <p
          className={`text-sm ${
            importNotice.kind === "ok"
              ? "text-brand-600"
              : importNotice.kind === "warn"
                ? "text-honey-600"
                : "text-coral-600"
          }`}
        >
          {importNotice.text}
        </p>
      )}
      {importCsv.data && importCsv.data.errors.length > 0 && (
        <ul className="space-y-1 text-xs text-coral-600">
          {importCsv.data.errors.slice(0, 10).map((error, index) => (
            <li key={index}>{error}</li>
          ))}
        </ul>
      )}

      {overview.isLoading && (
        <p className="text-sm text-ink-500">加载中…</p>
      )}
      {overview.isError && (
        <InlineError>{errorText(overview.error)}</InlineError>
      )}

      {overview.data && (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {totals.map((item) => (
              <div
                key={item.label}
                className="card p-4"
              >
                <p className="text-xs text-ink-500">{item.label}</p>
                <p className="mt-1 text-lg font-semibold">
                  {item.value.toLocaleString("zh-CN")}
                </p>
              </div>
            ))}
            <div className="col-span-2 rounded-xl border border-brand-100 bg-surface p-4 sm:col-span-3 lg:col-span-6">
              <p className="text-xs text-ink-500">互动率</p>
              <p className="mt-1 text-lg font-semibold text-brand-700">
                {(overview.data.engagement_rate * 100).toFixed(2)}%
              </p>
            </div>
          </section>

          {insights.isError && (
            <InlineError>{errorText(insights.error)}</InlineError>
          )}

          {channels.length > 0 && (
            <section className="grid gap-4 lg:grid-cols-3">
              <div className="card p-5 lg:col-span-2">
                <div className="flex items-baseline justify-between">
                  <h2 className="font-semibold">渠道表现</h2>
                  <span className="text-xs text-ink-500">按阅读量排序</span>
                </div>
                <ul className="mt-4 space-y-4">
                  {channels.map((channel) => (
                    <li key={channel.platform}>
                      <div className="flex flex-wrap items-baseline justify-between gap-1 text-sm">
                        <span className="font-medium text-ink-900">
                          {platformLabel(channel.platform)}
                        </span>
                        <span className="text-xs text-ink-500">
                          {channel.contents} 篇 · 互动率{" "}
                          {engagementPercent(channel.engagement_rate)}
                        </span>
                      </div>
                      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-brand-50">
                        <div
                          aria-hidden="true"
                          className="h-full rounded-full bg-brand-500"
                          style={{
                            width: `${sharePercent(channel.views, topChannelViews)}%`,
                          }}
                        />
                      </div>
                      <p className="mt-1 text-xs text-ink-500">
                        {channel.views.toLocaleString("zh-CN")} 阅读 ·{" "}
                        {channel.interactions.toLocaleString("zh-CN")} 次互动
                      </p>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="card flex flex-col p-5">
                <div className="flex items-baseline justify-between">
                  <h2 className="font-semibold">最佳发布时间</h2>
                  <span
                    className={
                      hasTimeData
                        ? "badge bg-mint-50 text-mint-700"
                        : "badge bg-honey-50 text-honey-700"
                    }
                  >
                    {hasTimeData ? "基于历史" : "通用建议"}
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {bestSlots.map((slot) => (
                    <span
                      key={`${slot.weekday ?? "any"}-${slot.hour}`}
                      className="rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-medium text-brand-700"
                    >
                      {slotLabel(slot.weekday, slot.hour)}
                    </span>
                  ))}
                </div>
                <p className="mt-auto pt-3 text-xs leading-relaxed text-ink-500">
                  {hasTimeData
                    ? `根据 ${publishedContents} 篇按时段发布的内容表现排序，样本越多越准。`
                    : "还没有可参考的发布历史，先给出通用建议时段；导入数据并完成发布后会自动按实际表现推荐。"}
                </p>
              </div>
            </section>
          )}

          {rankedContents.length > 0 && (
            <section className="overflow-x-auto card">
              <div className="flex items-baseline justify-between px-4 pt-4">
                <h2 className="font-semibold">内容排行</h2>
                <span className="text-xs text-ink-500">按互动量排序</span>
              </div>
              <table className="table1 w-full min-w-[560px] text-left">
                <thead className="border-b border-line-strong">
                  <tr>
                    <th className="px-4 py-3 font-medium">#</th>
                    <th className="px-4 py-3 font-medium">内容</th>
                    <th className="px-4 py-3 font-medium">平台</th>
                    <th className="px-4 py-3 font-medium">阅读</th>
                    <th className="px-4 py-3 font-medium">互动</th>
                    <th className="px-4 py-3 font-medium">互动率</th>
                  </tr>
                </thead>
                <tbody>
                  {rankedContents.map((item, index) => (
                    <tr
                      key={item.content_id}
                      className="border-b border-line last:border-0"
                    >
                      <td className="px-4 py-3 text-ink-500">{index + 1}</td>
                      <td className="px-4 py-3 font-medium">
                        {item.title || "未命名内容"}
                      </td>
                      <td className="px-4 py-3 text-ink-700">
                        {platformLabel(item.platform)}
                      </td>
                      <td className="px-4 py-3">
                        {item.views.toLocaleString("zh-CN")}
                      </td>
                      <td className="px-4 py-3">
                        {item.interactions.toLocaleString("zh-CN")}
                      </td>
                      <td className="px-4 py-3 text-ink-700">
                        {engagementPercent(item.engagement_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {hasTimeData && (
            <section className="overflow-x-auto card">
              <div className="flex items-baseline justify-between px-4 pt-4">
                <h2 className="font-semibold">时段表现</h2>
                <span className="text-xs text-ink-500">
                  按平均互动排序，最多展示 8 个时段
                </span>
              </div>
              <table className="table1 w-full min-w-[520px] text-left">
                <thead className="border-b border-line-strong">
                  <tr>
                    <th className="px-4 py-3 font-medium">时段</th>
                    <th className="px-4 py-3 font-medium">样本</th>
                    <th className="px-4 py-3 font-medium">平均阅读</th>
                    <th className="px-4 py-3 font-medium">平均互动</th>
                    <th className="px-4 py-3 font-medium">互动率</th>
                  </tr>
                </thead>
                <tbody>
                  {timeBuckets.map((bucket) => (
                    <tr
                      key={`${bucket.weekday}-${bucket.hour}`}
                      className="border-b border-line last:border-0"
                    >
                      <td className="px-4 py-3 font-medium">
                        {slotLabel(bucket.weekday, bucket.hour)}
                      </td>
                      <td className="px-4 py-3 text-ink-500">
                        {bucket.samples}
                      </td>
                      <td className="px-4 py-3">
                        {bucket.avg_views.toLocaleString("zh-CN")}
                      </td>
                      <td className="px-4 py-3">
                        {bucket.avg_interactions.toLocaleString("zh-CN")}
                      </td>
                      <td className="px-4 py-3 text-ink-700">
                        {engagementPercent(bucket.engagement_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          <section className="overflow-x-auto card">
            <div className="flex items-baseline justify-between px-4 pt-4">
              <h2 className="font-semibold">全部内容</h2>
              <span className="text-xs text-ink-500">查看采集时间并生成复盘</span>
            </div>
            <table className="table1 w-full min-w-[560px] text-left">
              <thead className="border-b border-line-strong">
                <tr>
                  <th className="px-4 py-3 font-medium">内容</th>
                  <th className="px-4 py-3 font-medium">平台</th>
                  <th className="px-4 py-3 font-medium">阅读</th>
                  <th className="px-4 py-3 font-medium">点赞</th>
                  <th className="px-4 py-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {overview.data.items.length === 0 && (
                  <tr>
                    <td
                      className="px-4 py-4 text-ink-500"
                      colSpan={5}
                    >
                      暂无内容。
                    </td>
                  </tr>
                )}
                {overview.data.items.map((item) => {
                  const feedback =
                    postmortemFeedback?.contentId === item.content_id
                      ? postmortemFeedback
                      : null;
                  const pending =
                    (postmortem.isPending &&
                      postmortem.variables === item.content_id) ||
                    (remove.isPending && remove.variables === item.content_id);
                  return (
                    <tr
                      key={item.content_id}
                      className="border-b border-line last:border-0"
                    >
                      <td className="px-4 py-3">
                        <span className="font-medium">
                          {item.title || "未命名内容"}
                        </span>
                        <span className="mt-1 block text-xs text-ink-500">
                          {item.has_data && item.captured_at
                            ? `采集于 ${item.captured_at}`
                            : "暂无数据"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-ink-700">
                        {platformLabel(item.platform)}
                      </td>
                      <td className="px-4 py-3">
                        {item.has_data
                          ? item.views.toLocaleString("zh-CN")
                          : "—"}
                      </td>
                      <td className="px-4 py-3">
                        {item.has_data
                          ? item.likes.toLocaleString("zh-CN")
                          : "—"}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          className="rounded border border-line px-3 py-1.5 text-xs text-ink-700 hover:border-ink-300 disabled:opacity-50"
                          disabled={
                            !canWrite || !item.has_data || postmortem.isPending
                          }
                          title={
                            !canWrite
                              ? "只读成员不能生成复盘"
                              : item.has_data
                                ? undefined
                                : "请先导入该内容的发布数据"
                          }
                          onClick={() => {
                            setPostmortemFeedback(null);
                            postmortem.mutate(item.content_id);
                          }}
                        >
                          {pending ? "复盘中…" : "复盘"}
                        </button>
                        {canWrite &&
                          (deletingId === item.content_id ? (
                            <span className="mt-1 flex flex-wrap items-center gap-1 text-xs text-coral-600">
                              删除后无法恢复
                              <button
                                type="button"
                                className="rounded bg-coral-600 px-2 py-1 font-medium text-white hover:bg-coral-700 disabled:opacity-50"
                                disabled={remove.isPending}
                                onClick={() => remove.mutate(item.content_id)}
                              >
                                {remove.isPending ? "删除中…" : "确认"}
                              </button>
                              <button
                                type="button"
                                className="rounded px-1 py-1 text-ink-500 hover:text-ink-900"
                                onClick={() => setDeletingId("")}
                              >
                                取消
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="mt-1 rounded border border-coral-100 px-3 py-1.5 text-xs text-coral-600 hover:border-coral-600 disabled:opacity-50"
                              disabled={pending}
                              onClick={() => {
                                setDeletingId(item.content_id);
                                setPostmortemFeedback(null);
                              }}
                            >
                              删除
                            </button>
                          ))}
                        {feedback && (
                          <p
                            className={`mt-1 text-xs ${
                              feedback.kind === "ok"
                                ? "text-brand-600"
                                : "text-coral-600"
                            }`}
                          >
                            {feedback.text}
                          </p>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}
