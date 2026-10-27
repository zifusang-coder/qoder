import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, useState } from "react";
import { Link } from "react-router-dom";
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
        <p className="mt-1 text-sm text-slate-400">
          导入发布数据，查看项目概览并生成单篇复盘。
        </p>
      </div>

      <div className="grid gap-4 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-2">
        <label className="space-y-1 text-sm text-slate-400">
          <span>项目</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
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
        <div className="space-y-1 text-sm text-slate-400">
          <span>导入 CSV</span>
          <div className="flex items-center gap-2">
            <input
              className="min-w-0 flex-1 text-sm text-slate-300 file:mr-3 file:rounded file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-slate-200"
              type="file"
              accept=".csv"
              aria-label="导入 CSV 文件"
              onChange={chooseFile}
            />
            <button
              type="button"
              className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
              disabled={!file || importCsv.isPending}
              onClick={submitImport}
            >
              {importCsv.isPending ? "导入中…" : "导入"}
            </button>
          </div>
        </div>
      </div>

      {projects.isError && (
        <p className="text-sm text-red-400">{errorText(projects.error)}</p>
      )}
      {projects.isSuccess && projects.data.items.length === 0 && (
        <p className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
          还没有项目，请先
          <Link className="ml-1 text-indigo-400" to="/projects">
            创建项目
          </Link>
          。
        </p>
      )}

      {importNotice && (
        <p
          className={`text-sm ${
            importNotice.kind === "ok"
              ? "text-emerald-400"
              : importNotice.kind === "warn"
                ? "text-amber-400"
                : "text-red-400"
          }`}
        >
          {importNotice.text}
        </p>
      )}
      {importCsv.data && importCsv.data.errors.length > 0 && (
        <ul className="space-y-1 text-xs text-red-400">
          {importCsv.data.errors.slice(0, 10).map((error, index) => (
            <li key={index}>{error}</li>
          ))}
        </ul>
      )}

      {overview.isLoading && (
        <p className="text-sm text-slate-400">加载中…</p>
      )}
      {overview.isError && (
        <p className="text-sm text-red-400">{errorText(overview.error)}</p>
      )}

      {overview.data && (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {totals.map((item) => (
              <div
                key={item.label}
                className="rounded-xl border border-slate-800 bg-slate-900 p-4"
              >
                <p className="text-xs text-slate-500">{item.label}</p>
                <p className="mt-1 text-lg font-semibold">
                  {item.value.toLocaleString("zh-CN")}
                </p>
              </div>
            ))}
            <div className="col-span-2 rounded-xl border border-indigo-900 bg-slate-900 p-4 sm:col-span-3 lg:col-span-6">
              <p className="text-xs text-slate-500">互动率</p>
              <p className="mt-1 text-lg font-semibold text-indigo-400">
                {(overview.data.engagement_rate * 100).toFixed(2)}%
              </p>
            </div>
          </section>

          <section className="overflow-x-auto rounded-xl border border-slate-800 bg-slate-900">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead className="border-b border-slate-800 text-xs text-slate-500">
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
                      className="px-4 py-4 text-slate-400"
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
                      className="border-b border-slate-800 last:border-0"
                    >
                      <td className="px-4 py-3">
                        <span className="font-medium">
                          {item.title || "未命名内容"}
                        </span>
                        <span className="mt-1 block text-xs text-slate-500">
                          {item.has_data && item.captured_at
                            ? `采集于 ${item.captured_at}`
                            : "暂无数据"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-300">
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
                          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-200 hover:border-slate-600 disabled:opacity-50"
                          disabled={!item.has_data || postmortem.isPending}
                          title={
                            item.has_data
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
                                ? "text-emerald-400"
                                : "text-red-400"
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
