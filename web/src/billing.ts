export interface PlanOut {
  code: string;
  name: string;
  description: string;
  price_cents: number;
  currency: string;
  limits: Record<string, number>;
  prices: Record<string, number>;
  status: string;
  updated_at: string;
  plan_changed_at: string | null;
}

export interface UsageRowOut {
  dimension: string;
  label: string;
  unit: string;
  unit_size: number;
  used: number;
  limit: number | null;
  remaining: number | null;
  percent: number | null;
  over_limit: boolean;
  price_cents: number;
  amount_cents: number;
}

export interface UsageOut {
  period: string;
  period_start: string;
  period_end: string;
  plan: PlanOut;
  rows: UsageRowOut[];
  amount_cents: number;
}

export interface StatementOut {
  period: string;
  status: string;
  plan_code: string;
  plan_name: string;
  plan_price_cents: number;
  currency: string;
  limits: Record<string, number>;
  prices: Record<string, number>;
  usage: Record<string, number>;
  charges: Array<{
    dimension: string;
    used: number;
    included: number;
    overage: number;
    unit_size: number;
    price_cents: number;
    amount_cents: number;
  }>;
  amount_cents: number;
  closed_at: string | null;
  reopened_at: string | null;
}

export interface PlatformUsageRow {
  tenant_id: string;
  tenant_name: string;
  tenant_slug: string;
  plan_code: string;
  plan_name: string;
  prompt_tokens: number;
  completion_tokens: number;
  amount_cents: number;
  statement_status: string | null;
}

export function formatCents(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export function formatTokens(value: number): string {
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return value.toLocaleString("zh-CN");
}

export function formatSeconds(value: number): string {
  return `${(value / 60).toFixed(1)} 分钟`;
}

export function rowPercent(
  used: number,
  limit: number | null,
): number | null {
  if (limit === null) return null;
  if (limit <= 0) return used > 0 ? 100 : 0;
  return Math.round((used * 100) / limit);
}

export function isOverLimit(used: number, limit: number | null): boolean {
  return limit !== null && used > limit;
}

const STATUS_LABELS: Record<string, string> = {
  closed: "已关账",
  open: "重开中",
};

export function statementStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function periodLabel(period: string): string {
  const [year, month] = period.split("-");
  return `${year} 年 ${Number(month)} 月`;
}

export function yuanToCents(input: string): number {
  const value = Number(input);
  if (!Number.isFinite(value) || value < 0) return 0;
  return Math.round(value * 100);
}

export function centsToYuan(cents: number): string {
  const yuan = cents / 100;
  return Number.isInteger(yuan) ? String(yuan) : String(Number(yuan.toFixed(2)));
}

/** 与后端 app/billing/dimensions.py 的注册表保持一致，顺序即展示顺序。 */
export interface PlanDimension {
  key: string;
  label: string;
  unit: string;
  billable: boolean;
}

export const PLAN_DIMENSIONS: ReadonlyArray<PlanDimension> = [
  { key: "llm.prompt_tokens", label: "输入 token", unit: "token", billable: true },
  {
    key: "llm.completion_tokens",
    label: "输出 token",
    unit: "token",
    billable: true,
  },
  { key: "run.skill", label: "技能运行", unit: "次", billable: false },
  { key: "run.agent", label: "Agent 运行", unit: "次", billable: false },
  { key: "run.workflow", label: "工作流运行", unit: "次", billable: false },
  { key: "media.jobs", label: "媒体任务", unit: "次", billable: false },
  { key: "media.seconds", label: "媒体时长", unit: "秒", billable: false },
];

const DIMENSION_LABELS: Record<string, string> = Object.fromEntries(
  PLAN_DIMENSIONS.map((item) => [item.key, item.label]),
);

export function dimensionLabel(key: string): string {
  return DIMENSION_LABELS[key] ?? key;
}

/** 账单明细里的用量展示：媒体时长按分钟，其余按 token/次数格式化。 */
export function formatDimensionValue(dimension: string, value: number): string {
  return dimension === "media.seconds"
    ? formatSeconds(value)
    : formatTokens(value);
}

/**
 * 最近账期：当前月 + 前 12 个月（共 13 个），按北京时间（UTC+8）切月。
 * `now` 仅用于测试；不传时取浏览器当前时间。
 */
export function recentPeriods(now: Date = new Date()): string[] {
  const beijing = new Date(now.getTime() + 8 * 60 * 60 * 1000);
  const year = beijing.getUTCFullYear();
  const month = beijing.getUTCMonth();
  const items: string[] = [];
  for (let offset = 0; offset < 13; offset += 1) {
    const date = new Date(Date.UTC(year, month - offset, 1));
    const monthText = String(date.getUTCMonth() + 1).padStart(2, "0");
    items.push(`${date.getUTCFullYear()}-${monthText}`);
  }
  return items;
}

/** 以北京时间显示 ISO 时间，空值与非法值显示破折号。 */
export function formatBeijingDateTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const beijing = new Date(date.getTime() + 8 * 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return [
    `${beijing.getUTCFullYear()}-${pad(beijing.getUTCMonth() + 1)}-${pad(
      beijing.getUTCDate(),
    )}`,
    `${pad(beijing.getUTCHours())}:${pad(beijing.getUTCMinutes())}`,
  ].join(" ");
}

export interface FeeBreakdown {
  monthlyCents: number;
  overageCents: number;
  totalCents: number;
}

/** 费用拆分：月费 + 各计费行金额之和（后端只在可计价维度返回非零 amount_cents）。 */
export function feeBreakdown(
  monthlyCents: number,
  rows: ReadonlyArray<{ amount_cents: number }>,
): FeeBreakdown {
  const overageCents = rows.reduce(
    (sum, row) => sum + Math.max(0, row.amount_cents || 0),
    0,
  );
  return {
    monthlyCents,
    overageCents,
    totalCents: monthlyCents + overageCents,
  };
}

export interface PlanDraft {
  code: string;
  name: string;
  description: string;
  priceYuan: string;
  limits: Record<string, string>;
  prices: Record<string, string>;
  status: string;
}

export interface PlanPayload {
  code?: string;
  name: string;
  description: string;
  price_cents: number;
  limits: Record<string, number>;
  prices: Record<string, number>;
  status?: string;
}

export function emptyPlanDraft(): PlanDraft {
  return {
    code: "",
    name: "",
    description: "",
    priceYuan: "",
    limits: Object.fromEntries(PLAN_DIMENSIONS.map((item) => [item.key, ""])),
    prices: Object.fromEntries(
      PLAN_DIMENSIONS.filter((item) => item.billable).map((item) => [
        item.key,
        "",
      ]),
    ),
    status: "active",
  };
}

export function planToDraft(plan: PlanOut): PlanDraft {
  return {
    code: plan.code,
    name: plan.name,
    description: plan.description,
    priceYuan: centsToYuan(plan.price_cents),
    limits: Object.fromEntries(
      PLAN_DIMENSIONS.map((item) => [item.key, plan.limits[item.key]?.toString() ?? ""]),
    ),
    prices: Object.fromEntries(
      PLAN_DIMENSIONS.filter((item) => item.billable).map((item) => [
        item.key,
        plan.prices[item.key] === undefined
          ? ""
          : centsToYuan(plan.prices[item.key]),
      ]),
    ),
    status: plan.status,
  };
}

const PLAN_CODE_PATTERN = /^[a-z][a-z0-9-]{1,49}$/;

function parseAmount(
  input: string,
): { value: number | null; error: string | null } {
  const raw = input.trim();
  if (raw === "") return { value: null, error: null };
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) {
    return { value: null, error: "需为非负数字" };
  }
  return { value: yuanToCents(raw), error: null };
}

/**
 * 表单 → API 请求体。额度留空表示不限量（不写入 key），单价留空表示不收费；
 * token 单价以「元/千 token」输入，换算为后端使用的「分/千 token」。
 */
export function buildPlanPayload(
  draft: PlanDraft,
  mode: "create" | "edit",
): { payload: PlanPayload | null; error: string | null } {
  const code = draft.code.trim();
  const name = draft.name.trim();
  if (mode === "create" && !PLAN_CODE_PATTERN.test(code)) {
    return {
      payload: null,
      error: "套餐标识需以小写字母开头，仅含小写字母、数字与连字符（2-50 位）",
    };
  }
  if (!name) return { payload: null, error: "套餐名称不能为空" };

  const price = parseAmount(draft.priceYuan);
  if (price.error) return { payload: null, error: `月费${price.error}` };

  const limits: Record<string, number> = {};
  for (const dimension of PLAN_DIMENSIONS) {
    const raw = (draft.limits[dimension.key] ?? "").trim();
    if (raw === "") continue;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) {
      return {
        payload: null,
        error: `额度「${dimension.label}」需为非负整数`,
      };
    }
    limits[dimension.key] = value;
  }

  const prices: Record<string, number> = {};
  for (const dimension of PLAN_DIMENSIONS) {
    if (!dimension.billable) continue;
    const parsed = parseAmount(draft.prices[dimension.key] ?? "");
    if (parsed.error) {
      return {
        payload: null,
        error: `单价「${dimension.label}」${parsed.error}`,
      };
    }
    if (parsed.value !== null) prices[dimension.key] = parsed.value;
  }

  const payload: PlanPayload = {
    name,
    description: draft.description.trim(),
    price_cents: price.value ?? 0,
    limits,
    prices,
  };
  if (mode === "create") {
    payload.code = code;
  } else {
    payload.status = draft.status === "archived" ? "archived" : "active";
  }
  return { payload, error: null };
}
