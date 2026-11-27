import { describe, expect, it } from "vitest";
import {
  buildPlanPayload,
  centsToYuan,
  dimensionLabel,
  emptyPlanDraft,
  feeBreakdown,
  formatBeijingDateTime,
  formatCents,
  formatDimensionValue,
  formatSeconds,
  formatTokens,
  isOverLimit,
  periodLabel,
  PLAN_DIMENSIONS,
  planToDraft,
  recentPeriods,
  rowPercent,
  statementStatusLabel,
  yuanToCents,
  type PlanDraft,
} from "../billing";

describe("formatCents", () => {
  it("formats cents as yuan", () => {
    expect(formatCents(0)).toBe("¥0.00");
    expect(formatCents(9901)).toBe("¥99.01");
  });

  it("always keeps two decimals", () => {
    expect(formatCents(100)).toBe("¥1.00");
    expect(formatCents(5)).toBe("¥0.05");
    expect(formatCents(10)).toBe("¥0.10");
  });

  it("handles large amounts", () => {
    expect(formatCents(123456789)).toBe("¥1234567.89");
    expect(formatCents(100_000_000)).toBe("¥1000000.00");
  });
});

describe("formatTokens", () => {
  it("formats tokens with 万 above ten thousand", () => {
    expect(formatTokens(9999)).toBe("9,999");
    expect(formatTokens(12_000)).toBe("1.2 万");
  });

  it("keeps small values plain and grouped", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(1)).toBe("1");
    expect(formatTokens(1000)).toBe("1,000");
    expect(formatTokens(100)).toBe("100");
  });

  it("switches to 万 exactly at ten thousand", () => {
    expect(formatTokens(10_000)).toBe("1.0 万");
    expect(formatTokens(10_001)).toBe("1.0 万");
    expect(formatTokens(123_456)).toBe("12.3 万");
  });
});

describe("formatSeconds", () => {
  it("formats media seconds as minutes", () => {
    expect(formatSeconds(0)).toBe("0.0 分钟");
    expect(formatSeconds(750)).toBe("12.5 分钟");
  });

  it("rounds to one decimal and supports full hours", () => {
    expect(formatSeconds(30)).toBe("0.5 分钟");
    expect(formatSeconds(59)).toBe("1.0 分钟");
    expect(formatSeconds(3600)).toBe("60.0 分钟");
  });
});

describe("rowPercent", () => {
  it("computes percent and over-limit safely", () => {
    expect(rowPercent(1500, 1000)).toBe(150);
    expect(rowPercent(3, 0)).toBe(100);
    expect(rowPercent(0, 0)).toBe(0);
    expect(rowPercent(3, null)).toBeNull();
    expect(isOverLimit(1500, 1000)).toBe(true);
    expect(isOverLimit(3, null)).toBe(false);
  });

  it("rounds percentages", () => {
    expect(rowPercent(500, 1000)).toBe(50);
    expect(rowPercent(1000, 1000)).toBe(100);
    expect(rowPercent(0, 1000)).toBe(0);
    expect(rowPercent(1, 3)).toBe(33);
    expect(rowPercent(2, 3)).toBe(67);
  });

  it("treats non-positive limits as a hard cap", () => {
    expect(rowPercent(0, -5)).toBe(0);
    expect(rowPercent(3, -5)).toBe(100);
  });

  it("returns null when there is no limit at all", () => {
    expect(rowPercent(0, null)).toBeNull();
    expect(rowPercent(999_999, null)).toBeNull();
  });
});

describe("isOverLimit", () => {
  it("is strict about exceeding the limit", () => {
    expect(isOverLimit(1000, 1000)).toBe(false);
    expect(isOverLimit(1001, 1000)).toBe(true);
  });

  it("counts any usage against a zero limit", () => {
    expect(isOverLimit(0, 0)).toBe(false);
    expect(isOverLimit(1, 0)).toBe(true);
  });

  it("never reports over-limit without a limit", () => {
    expect(isOverLimit(0, null)).toBe(false);
    expect(isOverLimit(99_999, null)).toBe(false);
  });
});

describe("statementStatusLabel", () => {
  it("labels statement status and period", () => {
    expect(statementStatusLabel("closed")).toBe("已关账");
    expect(statementStatusLabel("open")).toBe("重开中");
    expect(statementStatusLabel("other")).toBe("other");
    expect(periodLabel("2026-11")).toBe("2026 年 11 月");
  });

  it("passes unknown statuses through", () => {
    expect(statementStatusLabel("")).toBe("");
    expect(statementStatusLabel("pending")).toBe("pending");
  });
});

describe("periodLabel", () => {
  it("drops leading zeros in the month", () => {
    expect(periodLabel("2026-01")).toBe("2026 年 1 月");
    expect(periodLabel("2025-12")).toBe("2025 年 12 月");
  });
});

describe("yuan/cents conversion", () => {
  it("converts yuan input to integer cents", () => {
    expect(yuanToCents("99")).toBe(9900);
    expect(yuanToCents("0.20")).toBe(20);
    expect(yuanToCents("abc")).toBe(0);
    expect(centsToYuan(9900)).toBe("99");
    expect(centsToYuan(20)).toBe("0.2");
  });

  it("rounds decimal yuan to the nearest cent", () => {
    expect(yuanToCents("19.99")).toBe(1999);
    expect(yuanToCents("12.34")).toBe(1234);
    expect(yuanToCents("1000.5")).toBe(100_050);
  });

  it("rejects empty, negative and non-finite input", () => {
    expect(yuanToCents("")).toBe(0);
    expect(yuanToCents("-5")).toBe(0);
    expect(yuanToCents("Infinity")).toBe(0);
    expect(yuanToCents("0")).toBe(0);
  });

  it("formats cents without trailing zeros", () => {
    expect(centsToYuan(0)).toBe("0");
    expect(centsToYuan(5)).toBe("0.05");
    expect(centsToYuan(100)).toBe("1");
    expect(centsToYuan(1999)).toBe("19.99");
    expect(centsToYuan(123_456)).toBe("1234.56");
  });

  it("round-trips decimals through both converters", () => {
    expect(centsToYuan(yuanToCents("19.99"))).toBe("19.99");
    expect(centsToYuan(yuanToCents("0.07"))).toBe("0.07");
    expect(yuanToCents(centsToYuan(123_456))).toBe(123_456);
    expect(yuanToCents(centsToYuan(5))).toBe(5);
  });
});

describe("formatBeijingDateTime", () => {
  it("formats UTC instants in Beijing time", () => {
    expect(formatBeijingDateTime("2026-10-31T16:00:00Z")).toBe(
      "2026-11-01 00:00",
    );
    expect(formatBeijingDateTime("2026-10-31T15:59:00Z")).toBe(
      "2026-10-31 23:59",
    );
    expect(formatBeijingDateTime("2026-01-01T00:00:00Z")).toBe(
      "2026-01-01 08:00",
    );
  });

  it("renders a dash for missing or invalid values", () => {
    expect(formatBeijingDateTime(null)).toBe("—");
    expect(formatBeijingDateTime("")).toBe("—");
    expect(formatBeijingDateTime("not-a-date")).toBe("—");
  });
});

describe("recentPeriods", () => {
  it("returns current month plus 12 prior months", () => {
    const periods = recentPeriods(new Date("2026-10-31T15:59:59Z"));
    expect(periods).toHaveLength(13);
    expect(periods[0]).toBe("2026-10");
    expect(periods[1]).toBe("2026-09");
    expect(periods[11]).toBe("2025-11");
    expect(periods[12]).toBe("2025-10");
  });

  it("rolls into the next month at the Beijing UTC+8 boundary", () => {
    expect(recentPeriods(new Date("2026-10-31T15:59:59Z"))[0]).toBe("2026-10");
    expect(recentPeriods(new Date("2026-10-31T16:00:00Z"))[0]).toBe("2026-11");
    expect(recentPeriods(new Date("2026-10-31T16:00:00Z"))[12]).toBe(
      "2025-11",
    );
  });

  it("keeps descending order across a year boundary", () => {
    const periods = recentPeriods(new Date("2026-01-15T00:00:00Z"));
    expect(periods[0]).toBe("2026-01");
    expect(periods[1]).toBe("2025-12");
    expect(periods[12]).toBe("2025-01");
    expect(new Set(periods).size).toBe(13);
    expect([...periods].sort().reverse()).toEqual(periods);
  });
});

describe("feeBreakdown", () => {
  it("sums overage rows on top of the monthly fee", () => {
    expect(
      feeBreakdown(9900, [{ amount_cents: 1 }, { amount_cents: 0 }, { amount_cents: 3 }]),
    ).toEqual({ monthlyCents: 9900, overageCents: 4, totalCents: 9904 });
  });

  it("handles empty rows and clamps negatives defensively", () => {
    expect(feeBreakdown(0, [])).toEqual({
      monthlyCents: 0,
      overageCents: 0,
      totalCents: 0,
    });
    expect(feeBreakdown(500, [{ amount_cents: -10 }]).overageCents).toBe(0);
  });
});

describe("dimension helpers", () => {
  it("mirrors the backend registry with seven dimensions", () => {
    expect(PLAN_DIMENSIONS).toHaveLength(7);
    expect(PLAN_DIMENSIONS.map((item) => item.key)).toEqual([
      "llm.prompt_tokens",
      "llm.completion_tokens",
      "run.skill",
      "run.agent",
      "run.workflow",
      "media.jobs",
      "media.seconds",
    ]);
    expect(
      PLAN_DIMENSIONS.filter((item) => item.billable).map((item) => item.key),
    ).toEqual(["llm.prompt_tokens", "llm.completion_tokens"]);
  });

  it("labels known dimensions and passes unknown keys through", () => {
    expect(dimensionLabel("llm.prompt_tokens")).toBe("输入 token");
    expect(dimensionLabel("x.y")).toBe("x.y");
    expect(formatDimensionValue("media.seconds", 750)).toBe("12.5 分钟");
    expect(formatDimensionValue("llm.prompt_tokens", 12000)).toBe("1.2 万");
  });
});

function draft(overrides: Partial<PlanDraft> = {}): PlanDraft {
  return { ...emptyPlanDraft(), ...overrides };
}

describe("buildPlanPayload", () => {
  it("builds a create body and converts yuan-per-1k-token prices to cents", () => {
    const { payload, error } = buildPlanPayload(
      draft({
        code: "starter",
        name: " 入门版 ",
        description: " 测试套餐 ",
        priceYuan: "99",
        limits: { "llm.prompt_tokens": "1000" },
        prices: {
          "llm.prompt_tokens": "0.20",
          "llm.completion_tokens": "0.03",
        },
      }),
      "create",
    );
    expect(error).toBeNull();
    expect(payload).toEqual({
      code: "starter",
      name: "入门版",
      description: "测试套餐",
      price_cents: 9900,
      limits: { "llm.prompt_tokens": 1000 },
      prices: {
        "llm.prompt_tokens": 20,
        "llm.completion_tokens": 3,
      },
    });
  });

  it("omits blank limits and prices and defaults price to zero", () => {
    const { payload } = buildPlanPayload(
      draft({ code: "empty", name: "空套餐" }),
      "create",
    );
    expect(payload).toMatchObject({
      code: "empty",
      price_cents: 0,
      limits: {},
      prices: {},
    });
    expect(payload && "status" in payload).toBe(false);
  });

  it("builds an edit body without code and with status", () => {
    const { payload } = buildPlanPayload(
      draft({ code: "free", name: "免费版", priceYuan: "0", status: "archived" }),
      "edit",
    );
    expect(payload).not.toBeNull();
    expect(payload && "code" in payload).toBe(false);
    expect(payload?.status).toBe("archived");
    expect(
      buildPlanPayload(draft({ code: "free", name: "免费版", status: "weird" }), "edit")
        .payload?.status,
    ).toBe("active");
  });

  it("rejects missing or malformed fields", () => {
    expect(buildPlanPayload(draft({ name: "无标识" }), "create").error).toContain(
      "套餐标识",
    );
    expect(
      buildPlanPayload(draft({ code: "Bad Code", name: "坏标识" }), "create")
        .error,
    ).toContain("套餐标识");
    expect(
      buildPlanPayload(draft({ code: "ok-code", name: "   " }), "create")
        .error,
    ).toContain("套餐名称");
    expect(
      buildPlanPayload(
        draft({ code: "ok-code", name: "价格", priceYuan: "-1" }),
        "create",
      ).error,
    ).toContain("月费");
    expect(
      buildPlanPayload(
        draft({
          code: "ok-code",
          name: "额度",
          limits: { "run.skill": "1.5" },
        }),
        "create",
      ).error,
    ).toContain("技能运行");
    expect(
      buildPlanPayload(
        draft({
          code: "ok-code",
          name: "单价",
          prices: { "llm.prompt_tokens": "-0.1" },
        }),
        "create",
      ).error,
    ).toContain("单价");
  });

  it("round-trips an existing plan into an editable draft", () => {
    const plan = {
      code: "basic",
      name: "基础版",
      description: "说明",
      price_cents: 9900,
      currency: "CNY",
      limits: { "llm.prompt_tokens": 2_000_000 },
      prices: { "llm.prompt_tokens": 1, "llm.completion_tokens": 3 },
      status: "active",
      updated_at: "2026-11-01T00:00:00Z",
      plan_changed_at: null,
    };
    const draftValue = planToDraft(plan);
    expect(draftValue).toMatchObject({
      code: "basic",
      name: "基础版",
      priceYuan: "99",
      status: "active",
    });
    expect(draftValue.limits["llm.prompt_tokens"]).toBe("2000000");
    expect(draftValue.limits["run.skill"]).toBe("");
    expect(draftValue.prices["llm.prompt_tokens"]).toBe("0.01");
    expect(draftValue.prices["llm.completion_tokens"]).toBe("0.03");
  });
});
