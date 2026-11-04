import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  buildMonthGrid,
  calendarItemDate,
  gridRange,
  monthLabel,
  type CalendarItem,
} from "../calendar";
import { api } from "../client";
import {
  errorText,
  jobStatusLabel,
  platformLabel,
  type CalendarOut,
} from "../types";

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
const MAX_VISIBLE = 3;

function itemTone(status: string): string {
  if (status === "published") {
    return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  }
  if (
    status === "precheck_failed" ||
    status === "export_failed" ||
    status === "publish_failed"
  ) {
    return "border-red-500/30 bg-red-500/10 text-red-300";
  }
  if (status === "awaiting_review" || status === "approved") {
    return "border-amber-500/30 bg-amber-500/10 text-amber-300";
  }
  if (status === "exported") {
    return "border-sky-500/30 bg-sky-500/10 text-sky-300";
  }
  return "border-indigo-500/30 bg-indigo-500/10 text-indigo-300";
}

function itemTime(item: CalendarItem): string {
  const date = calendarItemDate(item);
  if (!date) return "";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function CalendarChip({ item }: { item: CalendarItem }) {
  return (
    <Link
      to="/publish"
      title={`${item.project_title} · ${item.content_title} · ${platformLabel(item.platform)} · ${jobStatusLabel(item.status)}`}
      className={`block truncate rounded border px-1.5 py-1 text-xs transition-opacity hover:opacity-80 ${itemTone(item.status)}`}
    >
      <span className="font-mono">{itemTime(item)}</span>{" "}
      <span>{item.content_title}</span>
    </Link>
  );
}

export default function Calendar() {
  const now = new Date();
  const [cursor, setCursor] = useState({
    year: now.getFullYear(),
    month: now.getMonth(),
  });

  const range = gridRange(cursor.year, cursor.month);
  const calendar = useQuery({
    queryKey: ["calendar", cursor.year, cursor.month],
    queryFn: () =>
      api<CalendarOut>(
        `/calendar?start=${encodeURIComponent(range.start.toISOString())}&end=${encodeURIComponent(range.end.toISOString())}`,
      ),
  });

  const items = calendar.data?.items ?? [];
  const cells = buildMonthGrid(cursor.year, cursor.month, items);
  const monthItems = cells
    .filter((cell) => cell.inMonth)
    .flatMap((cell) => cell.items);
  const scheduledCount = monthItems.filter((item) => item.scheduled_at).length;
  const publishedCount = monthItems.filter(
    (item) => item.status === "published",
  ).length;

  function shift(months: number) {
    const next = new Date(cursor.year, cursor.month + months, 1);
    setCursor({ year: next.getFullYear(), month: next.getMonth() });
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">内容日历</h1>
          <p className="mt-1 text-sm text-slate-400">
            按发布时间查看定时任务与已发布内容
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 text-slate-300 hover:bg-slate-800"
            onClick={() => shift(-1)}
          >
            上个月
          </button>
          <span className="min-w-32 text-center font-medium">
            {monthLabel(cursor.year, cursor.month)}
          </span>
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 text-slate-300 hover:bg-slate-800"
            onClick={() => shift(1)}
          >
            下个月
          </button>
          <button
            type="button"
            className="rounded px-2 py-1.5 text-indigo-400 hover:text-indigo-300"
            onClick={() =>
              setCursor({
                year: now.getFullYear(),
                month: now.getMonth(),
              })
            }
          >
            回到本月
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-4 text-xs text-slate-400">
        <span>
          本月定时 <strong className="text-slate-200">{scheduledCount}</strong>{" "}
          项
        </span>
        <span>
          已发布 <strong className="text-slate-200">{publishedCount}</strong> 项
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-emerald-400" /> 已发布
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-amber-400" /> 待审核/已通过
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-red-400" /> 失败
        </span>
      </div>

      {calendar.isError && (
        <p className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {errorText(calendar.error)}
        </p>
      )}
      {calendar.isLoading && (
        <p className="text-sm text-slate-400">加载中…</p>
      )}

      <div className="hidden overflow-hidden rounded-xl border border-slate-800 md:grid md:grid-cols-7">
        {WEEKDAYS.map((day) => (
          <div
            key={day}
            className="border-b border-r border-slate-800 bg-slate-900 px-2 py-2 text-center text-xs text-slate-400"
          >
            {day}
          </div>
        ))}
        {cells.map((cell) => (
          <div
            key={cell.key}
            className={`min-h-28 border-b border-r border-slate-800 p-2 ${
              cell.inMonth ? "" : "bg-slate-950/50 text-slate-600"
            }`}
          >
            <div className="mb-2 flex items-center justify-between text-xs">
              <span
                className={
                  cell.isToday
                    ? "rounded-full bg-indigo-500 px-1.5 py-0.5 font-semibold text-white"
                    : ""
                }
              >
                {cell.date.getDate()}
              </span>
              {cell.isToday && (
                <span className="text-[10px] text-indigo-400">今天</span>
              )}
            </div>
            <div className="space-y-1">
              {cell.items.slice(0, MAX_VISIBLE).map((item) => (
                <CalendarChip key={item.job_id} item={item} />
              ))}
              {cell.items.length > MAX_VISIBLE && (
                <p className="px-1 text-[11px] text-slate-500">
                  另有 {cell.items.length - MAX_VISIBLE} 项
                </p>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="space-y-3 md:hidden">
        {cells
          .filter((cell) => cell.inMonth && cell.items.length > 0)
          .map((cell) => (
            <div
              key={cell.key}
              className="rounded-xl border border-slate-800 p-3"
            >
              <p
                className={`mb-2 text-xs ${
                  cell.isToday ? "text-indigo-400" : "text-slate-400"
                }`}
              >
                {cell.date.getMonth() + 1} 月 {cell.date.getDate()} 日
                {cell.isToday ? " · 今天" : ""}
              </p>
              <div className="space-y-1">
                {cell.items.map((item) => (
                  <CalendarChip key={item.job_id} item={item} />
                ))}
              </div>
            </div>
          ))}
        {!calendar.isLoading &&
          !calendar.isError &&
          cells
            .filter((cell) => cell.inMonth)
            .every((cell) => cell.items.length === 0) && (
            <p className="rounded-xl border border-dashed border-slate-800 p-6 text-center text-sm text-slate-500">
              本月暂无定时或已发布内容
            </p>
          )}
      </div>
    </section>
  );
}
