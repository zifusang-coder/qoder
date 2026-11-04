import { describe, expect, it } from "vitest";
import {
  buildMonthGrid,
  calendarItemDate,
  gridRange,
  monthLabel,
  type CalendarItem,
} from "../calendar";

function item(overrides: Partial<CalendarItem>): CalendarItem {
  return {
    job_id: "job-1",
    content_id: "content-1",
    project_id: "project-1",
    project_title: "项目",
    content_title: "内容",
    platform: "wechat",
    status: "draft",
    scheduled_at: null,
    published_at: null,
    account_name: null,
    ...overrides,
  };
}

function localIso(year: number, month: number, day: number, hour = 9): string {
  return new Date(year, month, day, hour).toISOString();
}

describe("gridRange", () => {
  it("covers a Monday-first six-week grid", () => {
    const { start, end } = gridRange(2026, 10);
    expect(start.getFullYear()).toBe(2026);
    expect(start.getMonth()).toBe(9);
    expect(start.getDate()).toBe(26);
    expect(end.getMonth()).toBe(11);
    expect(end.getDate()).toBe(7);
  });

  it("wraps across the year boundary", () => {
    const { start } = gridRange(2026, 11);
    expect(start.getFullYear()).toBe(2026);
    expect(start.getMonth()).toBe(10);
    const { start: nextStart } = gridRange(2027, 0);
    expect(nextStart.getFullYear()).toBe(2026);
    expect(nextStart.getMonth()).toBe(11);
  });
});

describe("buildMonthGrid", () => {
  it("returns 42 cells with weekday-aligned dates", () => {
    const cells = buildMonthGrid(2026, 10, []);
    expect(cells).toHaveLength(42);
    expect(cells[0].date.getDate()).toBe(26);
    expect(cells[0].date.getMonth()).toBe(9);
    expect(cells.filter((cell) => cell.inMonth)).toHaveLength(30);
    expect(cells[0].items).toEqual([]);
  });

  it("groups items by their local event day", () => {
    const scheduled = item({
      job_id: "scheduled",
      scheduled_at: localIso(2026, 10, 12, 10),
    });
    const published = item({
      job_id: "published",
      status: "published",
      scheduled_at: null,
      published_at: localIso(2026, 10, 13, 8),
    });
    const cells = buildMonthGrid(2026, 10, [scheduled, published]);
    const nov12 = cells.find(
      (cell) => cell.date.getMonth() === 10 && cell.date.getDate() === 12,
    );
    const nov13 = cells.find(
      (cell) => cell.date.getMonth() === 10 && cell.date.getDate() === 13,
    );
    expect(nov12?.items.map((entry) => entry.job_id)).toEqual(["scheduled"]);
    expect(nov13?.items.map((entry) => entry.job_id)).toEqual(["published"]);
  });

  it("marks today only on the matching local day", () => {
    const today = new Date(2026, 10, 15, 12);
    const cells = buildMonthGrid(2026, 10, [], today);
    expect(cells.filter((cell) => cell.isToday)).toHaveLength(1);
    expect(cells.find((cell) => cell.isToday)?.date.getDate()).toBe(15);
  });
});

describe("calendarItemDate", () => {
  it("prefers the scheduled time", () => {
    const value = item({
      scheduled_at: localIso(2026, 10, 12),
      published_at: localIso(2026, 10, 13),
    });
    expect(calendarItemDate(value)?.getDate()).toBe(12);
  });

  it("falls back to published time", () => {
    const value = item({ published_at: localIso(2026, 10, 13) });
    expect(calendarItemDate(value)?.getDate()).toBe(13);
  });

  it("prefers the actual publish time for published jobs", () => {
    const value = item({
      status: "published",
      scheduled_at: localIso(2026, 10, 12),
      published_at: localIso(2026, 10, 13),
    });
    expect(calendarItemDate(value)?.getDate()).toBe(13);
  });

  it("returns null without an event time", () => {
    expect(calendarItemDate(item({}))).toBeNull();
  });

  it("uses the published time when the schedule falls outside the grid", () => {
    const value = item({
      scheduled_at: localIso(2026, 9, 1),
      published_at: localIso(2026, 10, 13),
    });
    const start = new Date(2026, 9, 26);
    const end = new Date(2026, 11, 7);
    expect(calendarItemDate(value, { start, end })?.getMonth()).toBe(10);
    expect(calendarItemDate(value, { start, end })?.getDate()).toBe(13);
  });

  it("keeps the scheduled time when it falls inside the grid", () => {
    const value = item({
      scheduled_at: localIso(2026, 10, 12),
      published_at: localIso(2026, 10, 13),
    });
    const start = new Date(2026, 9, 26);
    const end = new Date(2026, 11, 7);
    expect(calendarItemDate(value, { start, end })?.getDate()).toBe(12);
  });
});

describe("monthLabel", () => {
  it("formats the month in Chinese", () => {
    expect(monthLabel(2026, 10)).toBe("2026 年 11 月");
  });
});
