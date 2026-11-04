import type { CalendarItem } from "./types";

export type { CalendarItem } from "./types";

export interface DayCell {
  date: Date;
  key: string;
  inMonth: boolean;
  isToday: boolean;
  items: CalendarItem[];
}

const GRID_DAYS = 42;

function dateKey(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
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

export function buildMonthGrid(
  year: number,
  month: number,
  items: CalendarItem[],
  today: Date = new Date(),
): DayCell[] {
  const { start, end } = gridRange(year, month);
  const byDay = new Map<string, CalendarItem[]>();
  for (const item of items) {
    const date = calendarItemDate(item, { start, end });
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
