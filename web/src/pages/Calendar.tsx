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
    return "border-sage-100 bg-sage-50 text-sage-600";
  }
  if (
    status === "precheck_failed" ||
    status === "export_failed" ||
    status === "publish_failed"
  ) {
    return "border-wine-100 bg-wine-50 text-wine-600";
  }
  if (status === "awaiting_review" || status === "approved") {
    return "border-wine-100 bg-wine-50 text-warm-600";
  }
  if (status === "exported") {
    return "border-sage-100 bg-sage-50 text-sage-700";
  }
  return "border-sage-100 bg-sage-50 text-sage-700";
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
          <p className="mt-1 text-sm text-ink-500">
            按发布时间查看定时任务与已发布内容
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <button
            type="button"
            className="rounded border border-line px-3 py-1.5 text-ink-700 hover:bg-sage-50"
            onClick={() => shift(-1)}
          >
            上个月
          </button>
          <span className="min-w-32 text-center font-medium">
            {monthLabel(cursor.year, cursor.month)}
          </span>
          <button
            type="button"
            className="rounded border border-line px-3 py-1.5 text-ink-700 hover:bg-sage-50"
            onClick={() => shift(1)}
          >
            下个月
          </button>
          <button
            type="button"
            className="rounded px-2 py-1.5 text-sage-700 hover:text-sage-700"
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

      <div className="flex flex-wrap gap-4 text-xs text-ink-500">
        <span>
          本月定时 <strong className="text-ink-700">{scheduledCount}</strong>{" "}
          项
        </span>
        <span>
          已发布 <strong className="text-ink-700">{publishedCount}</strong> 项
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-sage-600" /> 已发布
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-warm-600" /> 待审核/已通过
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-wine-500" /> 失败
        </span>
      </div>

      {calendar.isError && (
        <p className="rounded border border-wine-100 bg-wine-50 px-3 py-2 text-sm text-wine-600">
          {errorText(calendar.error)}
        </p>
      )}
      {calendar.isLoading && (
        <p className="text-sm text-ink-500">加载中…</p>
      )}

      <div className="hidden overflow-hidden rounded-xl border border-line md:grid md:grid-cols-7">
        {WEEKDAYS.map((day) => (
          <div
            key={day}
            className="border-b border-r border-line bg-surface px-2 py-2 text-center text-xs text-ink-500"
          >
            {day}
          </div>
        ))}
        {cells.map((cell) => (
          <div
            key={cell.key}
            className={`min-h-28 border-b border-r border-line p-2 ${
              cell.inMonth ? "" : "bg-ink-900/30 text-ink-500"
            }`}
          >
            <div className="mb-2 flex items-center justify-between text-xs">
              <span
                className={
                  cell.isToday
                    ? "rounded-full bg-sage-600 px-1.5 py-0.5 font-semibold text-white"
                    : ""
                }
              >
                {cell.date.getDate()}
              </span>
              {cell.isToday && (
                <span className="text-[10px] text-sage-700">今天</span>
              )}
            </div>
            <div className="space-y-1">
              {cell.items.slice(0, MAX_VISIBLE).map((item) => (
                <CalendarChip key={item.job_id} item={item} />
              ))}
              {cell.items.length > MAX_VISIBLE && (
                <p className="px-1 text-[11px] text-ink-500">
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
              className="rounded-xl border border-line p-3"
            >
              <p
                className={`mb-2 text-xs ${
                  cell.isToday ? "text-sage-700" : "text-ink-500"
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
            <p className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-ink-500">
              本月暂无定时或已发布内容
            </p>
          )}
      </div>
    </section>
  );
}
