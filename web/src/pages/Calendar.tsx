import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth-context";
import {
  MAX_VISIBLE_ITEMS,
  buildMonthGrid,
  buildWeekGrid,
  calendarItemDate,
  canDragItem,
  gridRange,
  itemStatusKind,
  monthLabel,
  moveScheduledAt,
  platformDot,
  shiftWeek,
  visibleItems,
  weekLabel,
  weekRange,
  type CalendarItem,
  type DayCell,
} from "../calendar";
import { api } from "../client";
import { InlineError } from "../components/EmptyState";
import { usePlatforms } from "../platforms";
import {
  errorText,
  jobStatusLabel,
  platformLabel,
  type CalendarOut,
} from "../types";

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
type CalendarView = "month" | "week";
type Notice = { kind: "ok" | "error"; text: string };

function itemTone(status: string): string {
  if (status === "published") {
    return "border-mint-100 bg-mint-50 text-mint-700";
  }
  if (
    status === "precheck_failed" ||
    status === "export_failed" ||
    status === "publish_failed"
  ) {
    return "border-coral-100 bg-coral-50 text-coral-600";
  }
  if (status === "awaiting_review" || status === "approved") {
    return "border-honey-100 bg-honey-50 text-honey-700";
  }
  return "border-brand-100 bg-brand-50 text-brand-700";
}

function itemTime(item: CalendarItem): string {
  const date = calendarItemDate(item);
  if (!date) return "";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function weekdayName(date: Date): string {
  return WEEKDAYS[(date.getDay() + 6) % 7];
}

function StatusMark({ status }: { status: string }) {
  switch (itemStatusKind(status)) {
    case "published":
      return (
        <span
          aria-label="已发布"
          className="shrink-0 text-[11px] font-semibold leading-none text-mint-700"
        >
          ✓
        </span>
      );
    case "failed":
      return (
        <span
          aria-label="失败"
          className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-coral-500 text-[9px] font-bold leading-none text-white"
        >
          !
        </span>
      );
    case "pending":
      return (
        <span
          aria-label="待审核"
          className="h-1.5 w-1.5 shrink-0 rounded-full bg-honey-600"
        />
      );
    case "active":
      return (
        <span
          aria-label="进行中"
          className="shrink-0 text-[11px] leading-none text-brand-600"
        >
          …
        </span>
      );
    default:
      return null;
  }
}

function CalendarChip({
  item,
  draggable,
  onDragStart,
  onDragEnd,
}: {
  item: CalendarItem;
  draggable: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
}) {
  const status = jobStatusLabel(item.status);
  const title = [
    item.project_title,
    item.content_title,
    platformLabel(item.platform),
    status,
    item.error,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Link
      to="/publish"
      title={title}
      draggable={draggable}
      onDragStart={(event) => {
        if (!draggable) return;
        event.dataTransfer.setData("text/plain", item.job_id);
        event.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      onDragEnd={onDragEnd}
      className={`flex items-center gap-1.5 overflow-hidden rounded-md border px-1.5 py-1 text-xs transition-opacity hover:opacity-80 ${itemTone(item.status)} ${
        draggable ? "cursor-grab active:cursor-grabbing" : ""
      }`}
    >
      <span
        aria-hidden="true"
        className={`h-4 w-1 shrink-0 rounded-full ${platformDot(item.platform)}`}
      />
      <span className="shrink-0 font-mono text-[11px] tabular-nums opacity-75">
        {itemTime(item)}
      </span>
      <span className="min-w-0 flex-1 truncate">{item.content_title}</span>
      <StatusMark status={item.status} />
    </Link>
  );
}

export default function Calendar() {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const now = new Date();
  const [view, setView] = useState<CalendarView>("month");
  const [cursor, setCursor] = useState({
    year: now.getFullYear(),
    month: now.getMonth(),
  });
  const [weekAnchor, setWeekAnchor] = useState<Date>(now);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [dragging, setDragging] = useState<CalendarItem | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const platforms = usePlatforms();

  const range =
    view === "month"
      ? gridRange(cursor.year, cursor.month)
      : weekRange(weekAnchor);

  const calendar = useQuery({
    queryKey: ["calendar", range.start.toISOString(), range.end.toISOString()],
    queryFn: () =>
      api<CalendarOut>(
        `/calendar?start=${encodeURIComponent(range.start.toISOString())}&end=${encodeURIComponent(range.end.toISOString())}`,
      ),
  });

  const move = useMutation({
    mutationFn: ({
      item,
      scheduledAt,
    }: {
      item: CalendarItem;
      scheduledAt: string;
    }) =>
      api<{ id: string; scheduled_at: string | null }>(
        `/publish/jobs/${item.job_id}/schedule`,
        {
          method: "PATCH",
          body: JSON.stringify({ scheduled_at: scheduledAt }),
        },
      ),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["calendar"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      const when = new Date(variables.scheduledAt).toLocaleString("zh-CN", {
        month: "long",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
      setNotice({
        kind: "ok",
        text: `已将「${variables.item.content_title}」改期到 ${when}`,
      });
    },
    onError: (err) => {
      setNotice({ kind: "error", text: errorText(err) });
    },
  });

  const items = calendar.data?.items ?? [];
  const cells =
    view === "month"
      ? buildMonthGrid(cursor.year, cursor.month, items)
      : buildWeekGrid(weekAnchor, items);
  const rangeItems = cells
    .filter((cell) => cell.inMonth)
    .flatMap((cell) => cell.items);
  const scheduledCount = rangeItems.filter((item) => item.scheduled_at).length;
  const publishedCount = rangeItems.filter(
    (item) => item.status === "published",
  ).length;
  const dragEnabled = isAdmin && !move.isPending;
  const isEmpty =
    !calendar.isLoading &&
    !calendar.isError &&
    cells
      .filter((cell) => cell.inMonth)
      .every((cell) => cell.items.length === 0);

  function step(delta: number) {
    if (view === "month") {
      const next = new Date(cursor.year, cursor.month + delta, 1);
      setCursor({ year: next.getFullYear(), month: next.getMonth() });
      return;
    }
    setWeekAnchor((anchor) => shiftWeek(anchor, delta));
  }

  function goToday() {
    const today = new Date();
    setCursor({ year: today.getFullYear(), month: today.getMonth() });
    setWeekAnchor(today);
  }

  function toggleCell(key: string) {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function dropOn(cell: DayCell, event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const item = dragging;
    setDragging(null);
    if (!isAdmin || !item) return;
    const scheduledAt = moveScheduledAt(item, cell.date);
    if (!scheduledAt) return;
    setNotice(null);
    move.mutate({ item, scheduledAt });
  }

  const dropProps = (cell: DayCell) => ({
    onDragOver: (event: DragEvent<HTMLDivElement>) => {
      if (dragEnabled && dragging) event.preventDefault();
    },
    onDrop: (event: DragEvent<HTMLDivElement>) => dropOn(cell, event),
  });

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">内容日历</h1>
          <p className="mt-1 text-sm text-ink-500">
            按月或按周查看定时任务与已发布内容
            {isAdmin ? "，拖拽卡片可直接改期" : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <div className="inline-flex rounded-xl border border-line bg-white p-0.5">
            {(["month", "week"] as const).map((option) => (
              <button
                key={option}
                type="button"
                className={`rounded-lg px-3 py-1 transition-colors ${
                  view === option
                    ? "bg-brand-600 text-white"
                    : "text-ink-500 hover:text-ink-900"
                }`}
                onClick={() => setView(option)}
              >
                {option === "month" ? "月" : "周"}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn-secondary whitespace-nowrap px-3 py-1.5"
            onClick={() => step(-1)}
          >
            {view === "month" ? "上个月" : "上一周"}
          </button>
          <span className="min-w-32 text-center font-medium">
            {view === "month"
              ? monthLabel(cursor.year, cursor.month)
              : weekLabel(weekAnchor)}
          </span>
          <button
            type="button"
            className="btn-secondary whitespace-nowrap px-3 py-1.5"
            onClick={() => step(1)}
          >
            {view === "month" ? "下个月" : "下一周"}
          </button>
          <button
            type="button"
            className="whitespace-nowrap px-2 py-1.5 text-brand-700 hover:underline"
            onClick={goToday}
          >
            {view === "month" ? "回到本月" : "回到本周"}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-ink-500">
        <span>
          排期 <strong className="text-ink-700">{scheduledCount}</strong> 项
        </span>
        <span>
          已发布 <strong className="text-ink-700">{publishedCount}</strong> 项
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-mint-600" /> 已发布
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-honey-600" /> 待审核/已通过
        </span>
        <span className="flex items-center gap-1">
          <i className="h-2 w-2 rounded-full bg-coral-500" /> 失败
        </span>
        <span className="hidden items-center gap-1 sm:flex">
          {platforms.data?.map((platform) => (
            <span key={platform.key} className="flex items-center gap-1">
              <i
                className={`h-2 w-2 rounded-full ${platformDot(platform.key)}`}
              />
              {platform.label}
            </span>
          ))}
        </span>
        {isAdmin && (
          <span className="text-ink-300">按住卡片拖到目标日期即可改期</span>
        )}
      </div>

      {notice && (
        <p
          className={`rounded-lg border px-3 py-2 text-sm ${
            notice.kind === "ok"
              ? "border-mint-100 bg-mint-50 text-mint-700"
              : "border-coral-100 bg-coral-50 text-coral-600"
          }`}
        >
          {notice.text}
        </p>
      )}
      {calendar.isError && <InlineError>{errorText(calendar.error)}</InlineError>}
      {calendar.isLoading && <p className="text-sm text-ink-500">加载中…</p>}

      {view === "month" ? (
        <div className="hidden overflow-hidden rounded-xl border border-line md:grid md:grid-cols-7">
          {WEEKDAYS.map((day) => (
            <div
              key={day}
              className="border-b border-r border-line bg-ink-900/5 px-2 py-2 text-center text-xs font-medium text-ink-500"
            >
              {day}
            </div>
          ))}
          {cells.map((cell) => {
            const { visible, hidden } = visibleItems(
              cell.items,
              Boolean(expanded[cell.key]),
            );
            return (
              <div
                key={cell.key}
                {...dropProps(cell)}
                className={`min-h-28 border-b border-r border-line p-2 transition-shadow ${
                  cell.inMonth ? "" : "bg-ink-900/5 text-ink-500"
                } ${
                  dragEnabled && dragging
                    ? "ring-2 ring-inset ring-brand-300"
                    : ""
                }`}
              >
                <div className="mb-2 flex items-center justify-between text-xs">
                  <span
                    className={
                      cell.isToday
                        ? "rounded-full bg-brand-600 px-1.5 py-0.5 font-semibold text-white"
                        : ""
                    }
                  >
                    {cell.date.getDate()}
                  </span>
                  {cell.isToday && (
                    <span className="text-[10px] text-brand-700">今天</span>
                  )}
                </div>
                <div className="space-y-1">
                  {visible.map((item) => (
                    <CalendarChip
                      key={item.job_id}
                      item={item}
                      draggable={dragEnabled && canDragItem(item)}
                      onDragStart={() => setDragging(item)}
                      onDragEnd={() => setDragging(null)}
                    />
                  ))}
                  {hidden > 0 && (
                    <button
                      type="button"
                      aria-expanded={Boolean(expanded[cell.key])}
                      className="px-1 text-[11px] text-brand-700 hover:underline"
                      onClick={() => toggleCell(cell.key)}
                    >
                      +{hidden} 项
                    </button>
                  )}
                  {hidden === 0 &&
                    expanded[cell.key] &&
                    cell.items.length > MAX_VISIBLE_ITEMS && (
                      <button
                        type="button"
                        aria-expanded="true"
                        className="px-1 text-[11px] text-ink-500 hover:underline"
                        onClick={() => toggleCell(cell.key)}
                      >
                        收起
                      </button>
                    )}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="hidden overflow-hidden rounded-xl border border-line md:grid md:grid-cols-7">
          {cells.map((cell) => (
            <div
              key={cell.key}
              {...dropProps(cell)}
              className={`min-h-64 border-r border-line p-2 transition-shadow ${
                cell.isToday ? "bg-brand-50/50" : ""
              } ${
                dragEnabled && dragging
                  ? "ring-2 ring-inset ring-brand-300"
                  : ""
              }`}
            >
              <div className="mb-2 flex items-center justify-between text-xs">
                <span className="text-ink-500">
                  周{weekdayName(cell.date)} · {cell.date.getDate()}
                </span>
                {cell.isToday && (
                  <span className="text-[10px] text-brand-700">今天</span>
                )}
              </div>
              <div className="space-y-1">
                {cell.items.map((item) => (
                  <CalendarChip
                    key={item.job_id}
                    item={item}
                    draggable={dragEnabled && canDragItem(item)}
                    onDragStart={() => setDragging(item)}
                    onDragEnd={() => setDragging(null)}
                  />
                ))}
                {cell.items.length === 0 && (
                  <p className="px-1 text-[11px] text-ink-300">暂无内容</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="space-y-3 md:hidden">
        {cells
          .filter((cell) => cell.inMonth && cell.items.length > 0)
          .map((cell) => (
            <div key={cell.key} className="card card-pad">
              <p
                className={`mb-2 text-xs ${
                  cell.isToday ? "text-brand-700" : "text-ink-500"
                }`}
              >
                {cell.date.getMonth() + 1} 月 {cell.date.getDate()} 日
                {view === "week" ? ` 周${weekdayName(cell.date)}` : ""}
                {cell.isToday ? " · 今天" : ""}
              </p>
              <div className="space-y-1">
                {cell.items.map((item) => (
                  <CalendarChip
                    key={item.job_id}
                    item={item}
                    draggable={false}
                    onDragStart={() => undefined}
                    onDragEnd={() => undefined}
                  />
                ))}
              </div>
            </div>
          ))}
        {isEmpty && (
          <div className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-ink-500">
            <p>{view === "month" ? "本月暂无排期" : "本周暂无排期"}</p>
            <Link
              className="mt-2 inline-block text-brand-700 hover:underline"
              to="/publish"
            >
              去发布页安排内容
            </Link>
          </div>
        )}
      </div>
    </section>
  );
}
