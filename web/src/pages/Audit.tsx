import { useQuery } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import {
  AUDIT_ACTION_OPTIONS,
  auditActionLabel,
  auditStatusLabel,
  buildAuditQuery,
  type AuditFilters,
} from "../audit";
import { api } from "../client";
import { errorText, type AuditLogList } from "../types";

const PAGE_SIZE = 50;

const EMPTY_FILTERS: AuditFilters = {
  action: "",
  status: "",
  actor_email: "",
  from: "",
  to: "",
};

export default function Audit() {
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);

  const logs = useQuery({
    queryKey: ["audit", applied, offset],
    queryFn: () =>
      api<AuditLogList>(
        `/audit-logs?${buildAuditQuery(applied, PAGE_SIZE, offset)}`,
      ),
  });

  function apply(event: FormEvent) {
    event.preventDefault();
    setApplied(filters);
    setOffset(0);
  }

  function reset() {
    setFilters(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setOffset(0);
  }

  const total = logs.data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">审计</h1>
        <p className="mt-1 text-sm text-slate-400">
          查看当前租户的安全与业务关键事件。审计只追加，不提供修改或删除。
        </p>
      </div>

      <form
        className="grid gap-3 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-3"
        onSubmit={apply}
      >
        <label className="space-y-1 text-sm text-slate-400">
          <span>动作</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.action}
            onChange={(e) => setFilters({ ...filters, action: e.target.value })}
          >
            <option value="">全部</option>
            {AUDIT_ACTION_OPTIONS.map((action) => (
              <option key={action} value={action}>
                {auditActionLabel(action)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>状态</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.status}
            onChange={(e) => setFilters({ ...filters, status: e.target.value })}
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failure">失败</option>
          </select>
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>操作人邮箱</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.actor_email}
            onChange={(e) =>
              setFilters({ ...filters, actor_email: e.target.value })
            }
            placeholder="精确匹配"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>开始时间</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            type="datetime-local"
            value={filters.from}
            onChange={(e) => setFilters({ ...filters, from: e.target.value })}
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>结束时间</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            type="datetime-local"
            value={filters.to}
            onChange={(e) => setFilters({ ...filters, to: e.target.value })}
          />
        </label>
        <div className="flex items-end gap-2">
          <button className="rounded bg-indigo-600 px-4 py-2 font-medium">
            查询
          </button>
          <button
            type="button"
            className="rounded border border-slate-700 px-4 py-2 text-slate-300"
            onClick={reset}
          >
            重置
          </button>
        </div>
      </form>

      {logs.isLoading && <p className="text-sm text-slate-400">加载中…</p>}
      {logs.isError && (
        <p className="text-sm text-red-400">{errorText(logs.error)}</p>
      )}
      {logs.isSuccess && logs.data.items.length === 0 && (
        <p className="text-sm text-slate-400">没有匹配的审计记录。</p>
      )}

      {logs.data && logs.data.items.length > 0 && (
        <section className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-900 text-xs text-slate-500">
              <tr>
                <th className="px-4 py-3">时间</th>
                <th className="px-4 py-3">操作人</th>
                <th className="px-4 py-3">动作</th>
                <th className="px-4 py-3">状态</th>
                <th className="px-4 py-3">资源</th>
                <th className="px-4 py-3">详情</th>
              </tr>
            </thead>
            <tbody className="bg-slate-950">
              {logs.data.items.map((row) => (
                <tr key={row.id} className="border-t border-slate-800">
                  <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                    {new Date(row.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-slate-300">
                    {row.actor_email}
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-slate-200">
                      {auditActionLabel(row.action)}
                    </span>
                    <span className="mt-1 block font-mono text-xs text-slate-500">
                      {row.action}
                    </span>
                  </td>
                  <td
                    className={`px-4 py-3 ${
                      row.status === "success"
                        ? "text-emerald-400"
                        : "text-red-400"
                    }`}
                  >
                    {auditStatusLabel(row.status)}
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {row.resource_type}
                    <span className="mt-1 block font-mono text-xs text-slate-500">
                      {row.resource_id || "—"}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {Object.keys(row.detail).length === 0 ? (
                      <span className="text-slate-500">—</span>
                    ) : (
                      <details>
                        <summary className="cursor-pointer text-indigo-400">
                          查看
                        </summary>
                        <pre className="mt-2 max-w-md overflow-x-auto rounded bg-slate-900 p-2 text-xs text-slate-300">
                          {JSON.stringify(row.detail, null, 2)}
                        </pre>
                      </details>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {logs.data && (
        <div className="flex items-center gap-3 text-sm text-slate-400">
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-50"
            disabled={!hasPrev}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            上一页
          </button>
          <span>
            共 {total} 条，当前 {total === 0 ? 0 : offset + 1}–
            {Math.min(offset + PAGE_SIZE, total)}
          </span>
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-50"
            disabled={!hasNext}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
