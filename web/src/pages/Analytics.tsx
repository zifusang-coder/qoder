import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth-context";
import { api } from "../client";
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">数据</h1>
        <p className="mt-1 text-sm text-ink-500">
          导入发布数据，查看项目概览并生成单篇复盘。
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
              className="min-w-0 flex-1 text-sm text-ink-700 file:mr-3 file:rounded file:border-0 file:bg-sage-50 file:px-3 file:py-1.5 file:text-ink-700"
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
        <p className="text-sm text-wine-600">{errorText(projects.error)}</p>
      )}
      {projects.isSuccess && projects.data.items.length === 0 && (
        <p className="card p-6 text-sm text-ink-500">
          还没有项目，请先
          <Link className="ml-1 text-sage-700" to="/projects">
            创建项目
          </Link>
          。
        </p>
      )}

      {importNotice && (
        <p
          className={`text-sm ${
            importNotice.kind === "ok"
              ? "text-sage-600"
              : importNotice.kind === "warn"
                ? "text-warm-600"
                : "text-wine-600"
          }`}
        >
          {importNotice.text}
        </p>
      )}
      {importCsv.data && importCsv.data.errors.length > 0 && (
        <ul className="space-y-1 text-xs text-wine-600">
          {importCsv.data.errors.slice(0, 10).map((error, index) => (
            <li key={index}>{error}</li>
          ))}
        </ul>
      )}

      {overview.isLoading && (
        <p className="text-sm text-ink-500">加载中…</p>
      )}
      {overview.isError && (
        <p className="text-sm text-wine-600">{errorText(overview.error)}</p>
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
            <div className="col-span-2 rounded-xl border border-sage-100 bg-surface p-4 sm:col-span-3 lg:col-span-6">
              <p className="text-xs text-ink-500">互动率</p>
              <p className="mt-1 text-lg font-semibold text-sage-700">
                {(overview.data.engagement_rate * 100).toFixed(2)}%
              </p>
            </div>
          </section>

          <section className="overflow-x-auto card">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead className="border-b border-line text-xs text-ink-500">
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
                    postmortem.isPending &&
                    postmortem.variables === item.content_id;
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
                        {feedback && (
                          <p
                            className={`mt-1 text-xs ${
                              feedback.kind === "ok"
                                ? "text-sage-600"
                                : "text-wine-600"
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
