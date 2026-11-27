import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, Fragment, useState } from "react";
import { useAuth } from "../auth-context";
import {
  buildPlanPayload,
  dimensionLabel,
  emptyPlanDraft,
  feeBreakdown,
  formatBeijingDateTime,
  formatCents,
  formatDimensionValue,
  formatSeconds,
  formatTokens,
  periodLabel,
  PLAN_DIMENSIONS,
  planToDraft,
  recentPeriods,
  rowPercent,
  statementStatusLabel,
  type PlanDraft,
  type PlanOut,
  type PlanPayload,
  type PlatformUsageRow,
  type StatementOut,
  type UsageOut,
} from "../billing";
import { api } from "../client";
import EmptyState, { InlineError } from "../components/EmptyState";
import { errorText } from "../types";

function PlanEditor({
  mode,
  draft,
  error,
  pending,
  onChange,
  onSubmit,
  onClose,
}: {
  mode: "create" | "edit";
  draft: PlanDraft;
  error: string | null;
  pending: boolean;
  onChange: (next: PlanDraft) => void;
  onSubmit: (event: FormEvent) => void;
  onClose: () => void;
}) {
  function update(patch: Partial<PlanDraft>) {
    onChange({ ...draft, ...patch });
  }

  function updateLimit(key: string, value: string) {
    onChange({ ...draft, limits: { ...draft.limits, [key]: value } });
  }

  function updatePrice(key: string, value: string) {
    onChange({ ...draft, prices: { ...draft.prices, [key]: value } });
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink-900/20">
      <aside
        aria-labelledby="plan-editor-title"
        aria-modal="true"
        className="h-full w-full max-w-xl overflow-y-auto bg-surface p-6"
        role="dialog"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
      >
        <form className="space-y-4" onSubmit={onSubmit}>
          <div className="flex items-center justify-between">
            <h2 id="plan-editor-title" className="text-lg font-semibold">
              {mode === "create" ? "新建套餐" : `编辑套餐 ${draft.code}`}
            </h2>
            <button
              className="text-sm text-ink-500"
              type="button"
              onClick={onClose}
            >
              关闭
            </button>
          </div>

          {error && <InlineError>{error}</InlineError>}

          <div className="grid gap-3 md:grid-cols-2">
            <label className="space-y-1 text-sm text-ink-500">
              <span>套餐标识 *</span>
              <input
                autoFocus
                className="input text-ink-900 disabled:bg-paper disabled:text-ink-500"
                disabled={mode === "edit"}
                maxLength={50}
                required
                value={draft.code}
                onChange={(event) => update({ code: event.target.value })}
                placeholder="例如：starter"
              />
              {mode === "edit" && (
                <span className="block text-xs text-ink-400">
                  套餐标识创建后不可修改。
                </span>
              )}
            </label>
            <label className="space-y-1 text-sm text-ink-500">
              <span>套餐名称 *</span>
              <input
                className="input text-ink-900"
                maxLength={100}
                required
                value={draft.name}
                onChange={(event) => update({ name: event.target.value })}
                placeholder="例如：入门版"
              />
            </label>
          </div>

          <label className="block space-y-1 text-sm text-ink-500">
            <span>描述</span>
            <textarea
              className="input text-ink-900"
              maxLength={500}
              rows={2}
              value={draft.description}
              onChange={(event) => update({ description: event.target.value })}
              placeholder="一句话说明该套餐的适用场景。"
            />
          </label>

          <div className="grid gap-3 md:grid-cols-2">
            <label className="space-y-1 text-sm text-ink-500">
              <span>月费（元）</span>
              <input
                className="input text-ink-900"
                inputMode="decimal"
                value={draft.priceYuan}
                onChange={(event) =>
                  update({ priceYuan: event.target.value })
                }
                placeholder="0"
              />
            </label>
            {mode === "edit" && (
              <label className="space-y-1 text-sm text-ink-500">
                <span>状态</span>
                <select
                  className="input text-ink-900"
                  value={draft.status}
                  onChange={(event) => update({ status: event.target.value })}
                >
                  <option value="active">启用</option>
                  <option value="archived">已归档</option>
                </select>
              </label>
            )}
          </div>

          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-ink-700">
              额度（留空表示不限）
            </legend>
            <div className="grid gap-3 md:grid-cols-2">
              {PLAN_DIMENSIONS.map((dimension) => (
                <label
                  key={dimension.key}
                  className="space-y-1 text-sm text-ink-500"
                >
                  <span>
                    {dimension.label}（{dimension.unit}）
                  </span>
                  <input
                    className="input text-ink-900"
                    inputMode="numeric"
                    value={draft.limits[dimension.key] ?? ""}
                    onChange={(event) =>
                      updateLimit(dimension.key, event.target.value)
                    }
                    placeholder="不限"
                  />
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-ink-700">
              单价（元/千 token，留空表示不收费）
            </legend>
            <div className="grid gap-3 md:grid-cols-2">
              {PLAN_DIMENSIONS.filter((item) => item.billable).map(
                (dimension) => (
                  <label
                    key={dimension.key}
                    className="space-y-1 text-sm text-ink-500"
                  >
                    <span>{dimension.label}</span>
                    <input
                      className="input text-ink-900"
                      inputMode="decimal"
                      value={draft.prices[dimension.key] ?? ""}
                      onChange={(event) =>
                        updatePrice(dimension.key, event.target.value)
                      }
                      placeholder="0"
                    />
                  </label>
                ),
              )}
            </div>
          </fieldset>

          <div className="flex justify-end gap-2">
            <button className="btn-secondary" type="button" onClick={onClose}>
              取消
            </button>
            <button className="btn-primary" disabled={pending} type="submit">
              {pending ? "保存中…" : "保存"}
            </button>
          </div>
        </form>
      </aside>
    </div>
  );
}

export default function Billing() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [period, setPeriod] = useState(() => recentPeriods()[0]);
  const [expandedPeriod, setExpandedPeriod] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [planDraft, setPlanDraft] = useState<PlanDraft | null>(null);
  const [planMode, setPlanMode] = useState<"create" | "edit">("create");
  const [planError, setPlanError] = useState<string | null>(null);
  const periods = recentPeriods();

  const usage = useQuery({
    queryKey: ["billing-usage", period],
    queryFn: () => api<UsageOut>(`/billing/usage?period=${period}`),
  });
  const statements = useQuery({
    queryKey: ["billing-statements"],
    queryFn: () => api<StatementOut[]>("/billing/statements"),
  });
  const plans = useQuery({
    queryKey: ["billing-plans"],
    queryFn: () => api<PlanOut[]>("/billing/plans"),
    enabled: Boolean(user?.is_platform_admin),
  });
  const overview = useQuery({
    queryKey: ["billing-overview", period],
    queryFn: () =>
      api<PlatformUsageRow[]>(`/billing/platform/usage?period=${period}`),
    enabled: Boolean(user?.is_platform_admin),
  });

  const close = useMutation({
    mutationFn: ({ tenantId, target }: { tenantId: string; target: string }) =>
      api<StatementOut>(
        `/billing/platform/statements/${tenantId}/${target}/close`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setMessage("账单已关账");
      qc.invalidateQueries({ queryKey: ["billing-overview"] });
      qc.invalidateQueries({ queryKey: ["billing-statements"] });
    },
    onError: (err) => setMessage(errorText(err)),
  });
  const reopen = useMutation({
    mutationFn: ({ tenantId, target }: { tenantId: string; target: string }) =>
      api<StatementOut>(
        `/billing/platform/statements/${tenantId}/${target}/reopen`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setMessage("账单已重开");
      qc.invalidateQueries({ queryKey: ["billing-overview"] });
      qc.invalidateQueries({ queryKey: ["billing-statements"] });
    },
    onError: (err) => setMessage(errorText(err)),
  });
  const savePlan = useMutation({
    mutationFn: ({
      mode,
      code,
      payload,
    }: {
      mode: "create" | "edit";
      code: string;
      payload: PlanPayload;
    }) =>
      api<PlanOut>(mode === "create" ? "/billing/plans" : `/billing/plans/${code}`, {
        method: mode === "create" ? "POST" : "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      setPlanDraft(null);
      setPlanError(null);
      setMessage("套餐已保存");
      qc.invalidateQueries({ queryKey: ["billing-plans"] });
      qc.invalidateQueries({ queryKey: ["billing-plan"] });
      qc.invalidateQueries({ queryKey: ["billing-overview"] });
      qc.invalidateQueries({ queryKey: ["billing-usage"] });
    },
    onError: (err) => setPlanError(errorText(err)),
  });

  function openCreatePlan() {
    setPlanMode("create");
    setPlanDraft(emptyPlanDraft());
    setPlanError(null);
  }

  function openEditPlan(plan: PlanOut) {
    setPlanMode("edit");
    setPlanDraft(planToDraft(plan));
    setPlanError(null);
  }

  function submitPlan(event: FormEvent) {
    event.preventDefault();
    if (!planDraft) return;
    const { payload, error } = buildPlanPayload(planDraft, planMode);
    if (!payload) {
      setPlanError(error ?? "表单不合法");
      return;
    }
    setPlanError(null);
    savePlan.mutate({
      mode: planMode,
      code: planDraft.code.trim(),
      payload,
    });
  }

  const fees = usage.data
    ? feeBreakdown(usage.data.plan.price_cents, usage.data.rows)
    : null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">账单</h1>
        <p className="mt-1 text-sm text-ink-500">
          按北京时间自然月计量；输入/输出 token 计费，运行次数与媒体用量仅作指标。
        </p>
      </div>

      <label className="flex items-center gap-3 text-sm text-ink-500">
        <span>账期</span>
        <select
          className="input w-40 text-ink-900"
          value={period}
          onChange={(event) => setPeriod(event.target.value)}
        >
          {periods.map((item) => (
            <option key={item} value={item}>
              {periodLabel(item)}
            </option>
          ))}
        </select>
      </label>

      {message && <p className="text-sm text-brand-600">{message}</p>}
      {usage.isError && <InlineError>{errorText(usage.error)}</InlineError>}
      {usage.isLoading && <p className="text-sm text-ink-500">加载中…</p>}

      {usage.data && (
        <>
          <section className="card p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h2 className="font-medium">{usage.data.plan.name}</h2>
                <p className="mt-1 text-xs text-ink-500">
                  {usage.data.plan.description}
                </p>
              </div>
              <div className="text-right text-sm text-ink-700">
                <p>月费 {formatCents(usage.data.plan.price_cents)}</p>
                <p className="mt-1 text-xs text-ink-500">
                  绑定时间{" "}
                  {formatBeijingDateTime(usage.data.plan.plan_changed_at)}
                </p>
              </div>
            </div>
          </section>

          <section className="card p-5">
            <h2 className="font-medium">本期用量</h2>
            <div className="mt-3 space-y-3">
              {usage.data.rows.map((row) => {
                const percent = rowPercent(row.used, row.limit);
                const usedText =
                  row.dimension === "media.seconds"
                    ? formatSeconds(row.used)
                    : formatTokens(row.used);
                const limitText =
                  row.limit === null
                    ? "不限"
                    : row.dimension === "media.seconds"
                      ? formatSeconds(row.limit)
                      : formatTokens(row.limit);
                return (
                  <div key={row.dimension}>
                    <div className="flex items-center justify-between text-sm">
                      <span className={row.over_limit ? "text-coral-600" : ""}>
                        {row.label}
                      </span>
                      <span className="text-xs text-ink-500">
                        {usedText} / {limitText}
                        {row.over_limit ? " · 已超额" : ""}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-paper">
                      <div
                        className={
                          row.over_limit
                            ? "h-full bg-coral-500"
                            : "h-full bg-brand-500"
                        }
                        style={{ width: `${Math.min(percent ?? 0, 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          {fees && (
            <section className="card p-5">
              <h2 className="font-medium">预计费用</h2>
              <dl className="mt-3 space-y-2 text-sm">
                <div className="flex items-center justify-between">
                  <dt className="text-ink-500">月费</dt>
                  <dd className="text-ink-700">
                    {formatCents(fees.monthlyCents)}
                  </dd>
                </div>
                <div className="flex items-center justify-between">
                  <dt className="text-ink-500">预计超额</dt>
                  <dd className="text-ink-700">
                    {formatCents(fees.overageCents)}
                  </dd>
                </div>
                <div className="flex items-center justify-between border-t border-line-strong pt-2">
                  <dt className="font-medium text-ink-900">合计</dt>
                  <dd className="font-medium text-ink-900">
                    {formatCents(fees.totalCents)}
                  </dd>
                </div>
              </dl>
            </section>
          )}

          <section className="space-y-2">
            <h2 className="font-medium">账单</h2>
            {statements.isError && (
              <InlineError>{errorText(statements.error)}</InlineError>
            )}
            {statements.data?.length === 0 && (
              <EmptyState
                title="还没有账单"
                hint="账期结束后由平台管理员关账生成。"
              />
            )}
            {statements.data && statements.data.length > 0 && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>账期</th>
                    <th>金额</th>
                    <th>状态</th>
                    <th>关账时间</th>
                  </tr>
                </thead>
                <tbody>
                  {statements.data.map((item) => {
                    const expanded = expandedPeriod === item.period;
                    const detailId = `statement-charges-${item.period}`;
                    return (
                      <Fragment key={item.period}>
                        <tr
                          className="cursor-pointer"
                          onClick={() =>
                            setExpandedPeriod(expanded ? null : item.period)
                          }
                        >
                          <td>
                            <button
                              aria-controls={detailId}
                              aria-expanded={expanded}
                              className="flex items-center gap-1 text-left"
                              type="button"
                              onClick={(event) => {
                                event.stopPropagation();
                                setExpandedPeriod(
                                  expanded ? null : item.period,
                                );
                              }}
                            >
                              <span aria-hidden="true">
                                {expanded ? "▾" : "▸"}
                              </span>
                              {periodLabel(item.period)}
                            </button>
                          </td>
                          <td>{formatCents(item.amount_cents)}</td>
                          <td>{statementStatusLabel(item.status)}</td>
                          <td>{item.closed_at?.slice(0, 10) ?? "—"}</td>
                        </tr>
                        {expanded && (
                          <tr id={detailId}>
                            <td className="bg-paper/60" colSpan={4}>
                              <p className="text-xs text-ink-500">
                                月费 {formatCents(item.plan_price_cents)}
                                {item.charges.length > 0
                                  ? " + 分项超额"
                                  : "，本期无超额费用"}
                              </p>
                              {item.charges.length > 0 && (
                                <ul className="mt-2 space-y-1">
                                  {item.charges.map((charge) => (
                                    <li
                                      key={charge.dimension}
                                      className="flex items-center justify-between text-xs text-ink-700"
                                    >
                                      <span>
                                        {dimensionLabel(charge.dimension)} ·
                                        用量{" "}
                                        {formatDimensionValue(
                                          charge.dimension,
                                          charge.used,
                                        )}
                                      </span>
                                      <span>
                                        {formatCents(charge.amount_cents)}
                                      </span>
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      {user?.is_platform_admin && (
        <>
          <section className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-medium">套餐管理</h2>
              <button
                className="btn-primary"
                type="button"
                onClick={openCreatePlan}
              >
                新建套餐
              </button>
            </div>
            {plans.isError && <InlineError>{errorText(plans.error)}</InlineError>}
            {plans.data?.length === 0 && (
              <EmptyState title="还没有套餐" hint="新建套餐后即可指派给租户。" />
            )}
            {plans.data && plans.data.length > 0 && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>标识</th>
                    <th>名称</th>
                    <th>月费</th>
                    <th>输入 token 单价</th>
                    <th>输出 token 单价</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {plans.data.map((plan) => (
                    <tr key={plan.code}>
                      <td>{plan.code}</td>
                      <td>{plan.name}</td>
                      <td>{formatCents(plan.price_cents)}</td>
                      <td>
                        {formatCents(plan.prices["llm.prompt_tokens"] ?? 0)} / 千
                        token
                      </td>
                      <td>
                        {formatCents(plan.prices["llm.completion_tokens"] ?? 0)} /
                        千 token
                      </td>
                      <td>
                        <span
                          className={
                            plan.status === "active"
                              ? "badge bg-brand-50 text-brand-700"
                              : "badge bg-paper text-ink-500"
                          }
                        >
                          {plan.status === "active" ? "启用" : "已归档"}
                        </span>
                      </td>
                      <td>
                        <button
                          className="rounded border border-line px-2 py-1 text-xs text-ink-700"
                          type="button"
                          onClick={() => openEditPlan(plan)}
                        >
                          编辑
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-ink-500">
              归档套餐不可再指派；已绑定租户不受影响。额度留空表示不限，token
              单价以元/千 token 填写。
            </p>
          </section>

          {planDraft && (
            <PlanEditor
              draft={planDraft}
              error={planError}
              mode={planMode}
              pending={savePlan.isPending}
              onChange={setPlanDraft}
              onClose={() => {
                setPlanDraft(null);
                setPlanError(null);
              }}
              onSubmit={submitPlan}
            />
          )}

          <section className="space-y-2">
            <h2 className="font-medium">
              跨租户用量（{periodLabel(period)}）
            </h2>
            {overview.isError && (
              <InlineError>{errorText(overview.error)}</InlineError>
            )}
            {overview.isLoading && (
              <p className="text-sm text-ink-500">加载中…</p>
            )}
            {overview.data?.length === 0 && (
              <EmptyState
                title="还没有租户用量"
                hint="租户产生调用后，这里会按账期汇总。"
              />
            )}
            {overview.data && overview.data.length > 0 && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>租户</th>
                    <th>套餐</th>
                    <th>输入 token</th>
                    <th>输出 token</th>
                    <th>预计金额</th>
                    <th>账单</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {overview.data.map((row) => (
                    <tr key={row.tenant_id}>
                      <td>{row.tenant_name}</td>
                      <td>{row.plan_name}</td>
                      <td>{formatTokens(row.prompt_tokens)}</td>
                      <td>{formatTokens(row.completion_tokens)}</td>
                      <td>{formatCents(row.amount_cents)}</td>
                      <td>
                        {row.statement_status
                          ? statementStatusLabel(row.statement_status)
                          : "未关账"}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="rounded border border-line px-2 py-1 text-xs text-ink-700"
                          disabled={close.isPending || reopen.isPending}
                          onClick={() => {
                            const action =
                              row.statement_status === "closed" ? reopen : close;
                            action.mutate({
                              tenantId: row.tenant_id,
                              target: period,
                            });
                          }}
                        >
                          {row.statement_status === "closed" ? "重开" : "关账"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-ink-500">
              输入 token 按千 token 计价；金额为「月费 + 超额费用」的整数分换算结果。
            </p>
          </section>
        </>
      )}
    </div>
  );
}
