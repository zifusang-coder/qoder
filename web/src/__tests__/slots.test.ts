import { describe, expect, it } from "vitest";
import {
  DEFAULT_SLOTS,
  SLOT_LEAD_MINUTES,
  dayLabel,
  slotSuggestions,
  slotToDate,
} from "../slots";

function at(year: number, month: number, day: number, hour: number, minute = 0) {
  return new Date(year, month, day, hour, minute, 0, 0);
}

describe("slotToDate", () => {
  it("applies the slot time onto the given day", () => {
    const date = slotToDate(at(2026, 10, 12, 8), "18:30");
    expect(date?.getFullYear()).toBe(2026);
    expect(date?.getMonth()).toBe(10);
    expect(date?.getDate()).toBe(12);
    expect(date?.getHours()).toBe(18);
    expect(date?.getMinutes()).toBe(30);
  });

  it("rejects malformed or out-of-range slots", () => {
    expect(slotToDate(at(2026, 10, 12, 8), "25:00")).toBeNull();
    expect(slotToDate(at(2026, 10, 12, 8), "09:75")).toBeNull();
    expect(slotToDate(at(2026, 10, 12, 8), "evening")).toBeNull();
    expect(slotToDate(at(2026, 10, 12, 8), "")).toBeNull();
  });
});

describe("dayLabel", () => {
  it("names the next three days in Chinese", () => {
    const today = at(2026, 10, 12, 9);
    expect(dayLabel(at(2026, 10, 12, 20), today)).toBe("今天");
    expect(dayLabel(at(2026, 10, 13, 9), today)).toBe("明天");
    expect(dayLabel(at(2026, 10, 14, 9), today)).toBe("后天");
    expect(dayLabel(at(2026, 10, 18, 9), today)).toBe("11 月 18 日");
  });
});

describe("slotSuggestions", () => {
  it("skips slots inside the lead window and recommends the next one", () => {
    const now = at(2026, 10, 12, 9, 5);
    const result = slotSuggestions(now);
    expect(result.find((entry) => entry.label === "今天 09:00")).toBeUndefined();
    expect(result[0].slot).toBe("12:00");
    expect(result[0].recommended).toBe(true);
    expect(result[0].label).toBe("今天 12:00");
  });

  it("keeps a slot that is exactly on the lead boundary", () => {
    const now = at(2026, 10, 12, 11, 30);
    const result = slotSuggestions(now);
    expect(result[0].slot).toBe("12:00");
    expect(result[0].scheduledAt).toBe(at(2026, 10, 12, 12).toISOString());
  });

  it("rolls over to the next day when today's slots have passed", () => {
    const now = at(2026, 10, 12, 22);
    const result = slotSuggestions(now);
    expect(result[0].label).toBe("明天 09:00");
    expect(result[0].recommended).toBe(true);
    expect(result.map((entry) => entry.dayLabel)).toContain("明天");
  });

  it("spans the configured number of days and only marks one recommendation", () => {
    const now = at(2026, 10, 12, 8);
    const result = slotSuggestions(now, DEFAULT_SLOTS, { days: 2 });
    expect(result).toHaveLength(8);
    expect(result.filter((entry) => entry.recommended)).toHaveLength(1);
    expect(result.map((entry) => entry.label)).toEqual([
      "今天 09:00",
      "今天 12:00",
      "今天 18:00",
      "今天 21:00",
      "明天 09:00",
      "明天 12:00",
      "明天 18:00",
      "明天 21:00",
    ]);
  });

  it("honors a custom lead time", () => {
    const now = at(2026, 10, 12, 8, 10);
    const result = slotSuggestions(now, ["09:00"], { leadMinutes: 60 });
    expect(result[0].label).toBe("明天 09:00");
    expect(result).toHaveLength(2);
  });

  it("returns an empty list when no slot fits", () => {
    expect(slotSuggestions(at(2026, 10, 12, 8), [], { days: 3 })).toEqual([]);
    expect(SLOT_LEAD_MINUTES).toBe(30);
  });
});
