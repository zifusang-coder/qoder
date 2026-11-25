import { describe, expect, it } from "vitest";
import {
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
    error: null,
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

describe("platformDot", () => {
  it("gives every supported platform its own accent", () => {
    expect(platformDot("wechat")).toBe("bg-mint-600");
    expect(platformDot("douyin")).toBe("bg-ink-900");
    expect(platformDot("toutiao")).toBe("bg-coral-500");
    expect(platformDot("baijiahao")).toBe("bg-brand-600");
  });

  it("falls back to the brand accent for unknown platforms", () => {
    expect(platformDot("mystery")).toBe("bg-brand-600");
  });
});

describe("itemStatusKind", () => {
  it("classifies published, failed, pending and running jobs", () => {
    expect(itemStatusKind("published")).toBe("published");
    expect(itemStatusKind("publish_failed")).toBe("failed");
    expect(itemStatusKind("precheck_failed")).toBe("failed");
    expect(itemStatusKind("export_failed")).toBe("failed");
    expect(itemStatusKind("awaiting_review")).toBe("pending");
    expect(itemStatusKind("approved")).toBe("pending");
    expect(itemStatusKind("exporting")).toBe("active");
    expect(itemStatusKind("publishing")).toBe("active");
    expect(itemStatusKind("draft")).toBe("idle");
    expect(itemStatusKind("exported")).toBe("idle");
  });
});

describe("visibleItems", () => {
  const items = [item({ job_id: "a" }), item({ job_id: "b" }), item({ job_id: "c" }), item({ job_id: "d" })];

  it("limits a collapsed cell and reports the hidden count", () => {
    const result = visibleItems(items, false, 3);
    expect(result.visible.map((entry) => entry.job_id)).toEqual(["a", "b", "c"]);
    expect(result.hidden).toBe(1);
  });

  it("shows everything once expanded", () => {
    const result = visibleItems(items, true, 3);
    expect(result.visible).toHaveLength(4);
    expect(result.hidden).toBe(0);
  });

  it("never hides anything when it fits", () => {
    const result = visibleItems(items.slice(0, 2), false, 3);
    expect(result.visible).toHaveLength(2);
    expect(result.hidden).toBe(0);
  });
});

describe("weekRange", () => {
  it("returns the Monday-first week containing the anchor", () => {
    const { start, end } = weekRange(new Date(2026, 10, 19, 12));
    expect(start.getFullYear()).toBe(2026);
    expect(start.getMonth()).toBe(10);
    expect(start.getDate()).toBe(16);
    expect(end.getDate()).toBe(23);
    expect(end.getTime() - start.getTime()).toBe(7 * 24 * 60 * 60 * 1000);
  });

  it("keeps Sunday in the week that started the previous Monday", () => {
    const { start } = weekRange(new Date(2026, 10, 22));
    expect(start.getDate()).toBe(16);
  });
});

describe("shiftWeek", () => {
  it("moves the anchor by whole weeks", () => {
    const anchor = new Date(2026, 10, 19);
    expect(shiftWeek(anchor, 1).getDate()).toBe(26);
    expect(shiftWeek(anchor, -1).getDate()).toBe(12);
  });
});

describe("weekLabel", () => {
  it("formats the covered date span", () => {
    expect(weekLabel(new Date(2026, 10, 19))).toBe("11 月 16 日 – 11 月 23 日");
  });
});

describe("buildWeekGrid", () => {
  it("returns seven Monday-first cells grouped by day", () => {
    const scheduled = item({
      job_id: "week",
      scheduled_at: localIso(2026, 10, 18, 10),
    });
    const cells = buildWeekGrid(new Date(2026, 10, 19), [scheduled]);
    expect(cells).toHaveLength(7);
    expect(cells[0].date.getDay()).toBe(1);
    expect(cells[2].date.getDate()).toBe(18);
    expect(cells[2].items.map((entry) => entry.job_id)).toEqual(["week"]);
    expect(cells.every((cell) => cell.inMonth)).toBe(true);
  });

  it("marks today inside the week", () => {
    const today = new Date(2026, 10, 19, 9);
    const cells = buildWeekGrid(today, [], today);
    expect(cells.filter((cell) => cell.isToday)).toHaveLength(1);
    expect(cells.find((cell) => cell.isToday)?.date.getDate()).toBe(19);
  });
});

describe("canDragItem", () => {
  it("allows scheduled jobs that can still be rescheduled", () => {
    expect(
      canDragItem(
        item({ status: "draft", scheduled_at: localIso(2026, 10, 12) }),
      ),
    ).toBe(true);
    expect(
      canDragItem(
        item({ status: "awaiting_review", scheduled_at: localIso(2026, 10, 12) }),
      ),
    ).toBe(true);
  });

  it("rejects published jobs and jobs without a schedule", () => {
    expect(
      canDragItem(
        item({ status: "published", published_at: localIso(2026, 10, 12) }),
      ),
    ).toBe(false);
    expect(canDragItem(item({ status: "draft" }))).toBe(false);
    expect(
      canDragItem(
        item({ status: "publishing", scheduled_at: localIso(2026, 10, 12) }),
      ),
    ).toBe(false);
  });
});

describe("moveScheduledAt", () => {
  it("keeps the original clock time on the target day", () => {
    const value = item({
      scheduled_at: new Date(2026, 10, 12, 10, 30).toISOString(),
    });
    const moved = moveScheduledAt(value, new Date(2026, 10, 20));
    expect(moved).not.toBeNull();
    const parsed = new Date(moved as string);
    expect(parsed.getFullYear()).toBe(2026);
    expect(parsed.getMonth()).toBe(10);
    expect(parsed.getDate()).toBe(20);
    expect(parsed.getHours()).toBe(10);
    expect(parsed.getMinutes()).toBe(30);
  });

  it("returns null when the item has no schedule", () => {
    expect(moveScheduledAt(item({}), new Date(2026, 10, 20))).toBeNull();
  });
});
