import { describe, expect, it } from "vitest";
import {
  engagementPercent,
  hourLabel,
  rankTimeBuckets,
  sharePercent,
  slotLabel,
  weekdayLabel,
  type TimeBucket,
} from "../analytics";

function bucket(
  weekday: number,
  hour: number,
  avgInteractions: number,
  avgViews: number,
  samples = 1,
): TimeBucket {
  return {
    weekday,
    hour,
    samples,
    avg_views: avgViews,
    avg_interactions: avgInteractions,
    engagement_rate: 0,
  };
}

describe("weekdayLabel", () => {
  it("maps Monday-based indexes to Chinese labels", () => {
    expect(weekdayLabel(0)).toBe("周一");
    expect(weekdayLabel(2)).toBe("周三");
    expect(weekdayLabel(6)).toBe("周日");
  });

  it("uses 每天 for fallback slots", () => {
    expect(weekdayLabel(null)).toBe("每天");
  });

  it("falls back to 每天 for out-of-range values", () => {
    expect(weekdayLabel(7)).toBe("每天");
    expect(weekdayLabel(-1)).toBe("每天");
  });
});

describe("hourLabel", () => {
  it("pads single-digit hours", () => {
    expect(hourLabel(9)).toBe("09:00");
    expect(hourLabel(0)).toBe("00:00");
    expect(hourLabel(21)).toBe("21:00");
  });
});

describe("slotLabel", () => {
  it("combines weekday and hour", () => {
    expect(slotLabel(2, 9)).toBe("周三 09:00");
    expect(slotLabel(null, 18)).toBe("每天 18:00");
  });
});

describe("engagementPercent", () => {
  it("renders two decimal places", () => {
    expect(engagementPercent(0.1545)).toBe("15.45%");
    expect(engagementPercent(0.12)).toBe("12.00%");
    expect(engagementPercent(0)).toBe("0.00%");
  });
});

describe("sharePercent", () => {
  it("scales values against the maximum", () => {
    expect(sharePercent(50, 200)).toBe(25);
    expect(sharePercent(200, 200)).toBe(100);
  });

  it("clamps negative and oversized values", () => {
    expect(sharePercent(-5, 100)).toBe(0);
    expect(sharePercent(150, 100)).toBe(100);
  });

  it("returns 0 when there is no maximum", () => {
    expect(sharePercent(10, 0)).toBe(0);
  });
});

describe("rankTimeBuckets", () => {
  it("sorts by average interactions then views", () => {
    const ranked = rankTimeBuckets([
      bucket(0, 10, 40, 500),
      bucket(0, 18, 85, 550),
      bucket(2, 9, 85, 900),
    ]);
    expect(ranked.map((item) => [item.weekday, item.hour])).toEqual([
      [2, 9],
      [0, 18],
      [0, 10],
    ]);
  });

  it("breaks full ties by weekday and hour", () => {
    const ranked = rankTimeBuckets([
      bucket(3, 12, 10, 100),
      bucket(1, 20, 10, 100),
      bucket(1, 8, 10, 100),
    ]);
    expect(ranked.map((item) => [item.weekday, item.hour])).toEqual([
      [1, 8],
      [1, 20],
      [3, 12],
    ]);
  });

  it("limits the number of buckets and keeps the input intact", () => {
    const input = [bucket(0, 1, 1, 1), bucket(1, 1, 5, 5), bucket(2, 1, 3, 3)];
    const ranked = rankTimeBuckets(input, 2);
    expect(ranked.map((item) => item.weekday)).toEqual([1, 2]);
    expect(input.map((item) => item.weekday)).toEqual([0, 1, 2]);
  });
});
