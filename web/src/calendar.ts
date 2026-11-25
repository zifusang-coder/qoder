import { canRescheduleStatus, type CalendarItem } from "./types";

export type { CalendarItem } from "./types";

export interface DayCell {
  date: Date;
  key: string;
  inMonth: boolean;
  isToday: boolean;
  items: CalendarItem[];
}

const GRID_DAYS = 42;
export const MAX_VISIBLE_ITEMS = 3;
export const WEEK_DAYS = 7;

const PLATFORM_DOTS: Record<string, string> = {
  wechat: "bg-mint-600",
  douyin: "bg-ink-900",
  toutiao: "bg-coral-500",
  baijiahao: "bg-brand-600",
};

export type StatusKind = "published" | "failed" | "pending" | "active" | "idle";

const FAILED_STATUSES = new Set([
  "precheck_failed",
  "export_failed",
  "publish_failed",
]);
const PENDING_STATUSES = new Set(["awaiting_review", "approved"]);
const ACTIVE_STATUSES = new Set(["prechecking", "exporting", "publishing"]);

export function platformDot(platform: string): string {
  return PLATFORM_DOTS[platform] ?? "bg-brand-600";
}

export function itemStatusKind(status: string): StatusKind {
  if (status === "published") return "published";
  if (FAILED_STATUSES.has(status)) return "failed";
  if (PENDING_STATUSES.has(status)) return "pending";
  if (ACTIVE_STATUSES.has(status)) return "active";
  return "idle";
}

export function visibleItems(
  items: CalendarItem[],
  expanded: boolean,
  limit: number = MAX_VISIBLE_ITEMS,
): { visible: CalendarItem[]; hidden: number } {
  const visible = expanded ? items : items.slice(0, limit);
  return { visible, hidden: items.length - visible.length };
}

function dateKey(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

export function weekRange(anchor: Date): { start: Date; end: Date } {
  const offset = (anchor.getDay() + 6) % 7;
  const start = new Date(
    anchor.getFullYear(),
    anchor.getMonth(),
    anchor.getDate() - offset,
  );
  const end = new Date(
    start.getFullYear(),
    start.getMonth(),
    start.getDate() + WEEK_DAYS,
  );
  return { start, end };
}

export function shiftWeek(anchor: Date, weeks: number): Date {
  return new Date(
    anchor.getFullYear(),
    anchor.getMonth(),
    anchor.getDate() + weeks * WEEK_DAYS,
  );
}

export function weekLabel(anchor: Date): string {
  const { start, end } = weekRange(anchor);
  return `${start.getMonth() + 1} 月 ${start.getDate()} 日 – ${end.getMonth() + 1} 月 ${end.getDate()} 日`;
}

export function monthLabel(year: number, month: number): string {
  return `${year} 年 ${month + 1} 月`;
}

/** Monday-first six-week range that fully contains the month. */
export function gridRange(
  year: number,
  month: number,
): { start: Date; end: Date } {
  const first = new Date(year, month, 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - offset);
  const end = new Date(year, month, 1 - offset + GRID_DAYS);
  return { start, end };
}

function parseDate(value: string | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function within(date: Date, range?: { start: Date; end: Date }): boolean {
  if (!range) return true;
  return date >= range.start && date < range.end;
}

export function calendarItemDate(
  item: CalendarItem,
  range?: { start: Date; end: Date },
): Date | null {
  const scheduled = parseDate(item.scheduled_at);
  const published = parseDate(item.published_at);
  const preferred =
    item.status === "published" ? (published ?? scheduled) : (scheduled ?? published);
  const alternate = preferred === scheduled ? published : scheduled;
  if (preferred && within(preferred, range)) return preferred;
  if (alternate && within(alternate, range)) return alternate;
  return preferred ?? alternate;
}

function bucketByDay(
  items: CalendarItem[],
  range: { start: Date; end: Date },
): Map<string, CalendarItem[]> {
  const byDay = new Map<string, CalendarItem[]>();
  for (const item of items) {
    const date = calendarItemDate(item, range);
    if (!date) continue;
    const key = dateKey(date);
    const bucket = byDay.get(key) ?? [];
    bucket.push(item);
    byDay.set(key, bucket);
  }
  for (const bucket of byDay.values()) {
    bucket.sort((left, right) => {
      const a = calendarItemDate(left)?.getTime() ?? 0;
      const b = calendarItemDate(right)?.getTime() ?? 0;
      return a - b;
    });
  }
  return byDay;
}

export function buildMonthGrid(
  year: number,
  month: number,
  items: CalendarItem[],
  today: Date = new Date(),
): DayCell[] {
  const { start, end } = gridRange(year, month);
  const byDay = bucketByDay(items, { start, end });

  return Array.from({ length: GRID_DAYS }, (_, index) => {
    const date = new Date(
      start.getFullYear(),
      start.getMonth(),
      start.getDate() + index,
    );
    const key = dateKey(date);
    return {
      date,
      key,
      inMonth: date.getMonth() === month && date.getFullYear() === year,
      isToday: dateKey(today) === key,
      items: byDay.get(key) ?? [],
    };
  });
}

export function buildWeekGrid(
  anchor: Date,
  items: CalendarItem[],
  today: Date = new Date(),
): DayCell[] {
  const { start, end } = weekRange(anchor);
  const byDay = bucketByDay(items, { start, end });

  return Array.from({ length: WEEK_DAYS }, (_, index) => {
    const date = new Date(
      start.getFullYear(),
      start.getMonth(),
      start.getDate() + index,
    );
    const key = dateKey(date);
    return {
      date,
      key,
      inMonth: true,
      isToday: dateKey(today) === key,
      items: byDay.get(key) ?? [],
    };
  });
}

export function canDragItem(item: CalendarItem): boolean {
  return Boolean(item.scheduled_at) && canRescheduleStatus(item.status);
}

export function moveScheduledAt(
  item: CalendarItem,
  target: Date,
): string | null {
  const scheduled = parseDate(item.scheduled_at);
  if (!scheduled) return null;
  return new Date(
    target.getFullYear(),
    target.getMonth(),
    target.getDate(),
    scheduled.getHours(),
    scheduled.getMinutes(),
    scheduled.getSeconds(),
  ).toISOString();
}
