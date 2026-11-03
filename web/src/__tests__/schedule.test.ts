import { describe, expect, it } from "vitest";
import { formatTime, toDatetimeLocal, toScheduledAt } from "../schedule";

describe("toScheduledAt", () => {
  it("returns null for an empty or invalid value", () => {
    expect(toScheduledAt("")).toBeNull();
    expect(toScheduledAt("not-a-date")).toBeNull();
  });

  it("converts a local datetime input to an ISO instant", () => {
    const local = "2026-10-28T09:30";
    expect(toScheduledAt(local)).toBe(new Date(local).toISOString());
  });
});

describe("toDatetimeLocal", () => {
  it("returns an empty string for an empty or invalid value", () => {
    expect(toDatetimeLocal(null)).toBe("");
    expect(toDatetimeLocal("not-a-date")).toBe("");
  });

  it("round-trips a minute-precision local input", () => {
    const local = "2026-10-28T09:30";
    expect(toDatetimeLocal(toScheduledAt(local))).toBe(local);
  });
});

describe("formatTime", () => {
  it("returns the original value when it cannot be parsed", () => {
    expect(formatTime("not-a-date")).toBe("not-a-date");
  });

  it("returns an empty string for a missing value", () => {
    expect(formatTime(null)).toBe("");
  });
});
