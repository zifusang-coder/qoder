import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  canEnableTenant,
  canSuspendTenant,
  tenantStatusLabel,
  validateTenantForm,
} from "../tenants";
import { errorText, type Tenant, type TenantCreated } from "../types";

const EMPTY_FORM = {
  name: "",
  slug: "",
  owner_email: "",
  owner_display_name: "",
};

export default function Tenants() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [form, setForm] = useState(EMPTY_FORM);
  const [created, setCreated] = useState<TenantCreated | null>(null);
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/tenants"),
  });

  const create = useMutation({
    mutationFn: (payload: typeof EMPTY_FORM) =>
      api<TenantCreated>("/tenants", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (tenant) => {
      setCreated(tenant);
      setForm(EMPTY_FORM);
      setMessage({ kind: "ok", text: `已创建租户 ${tenant.name}` });
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: () => setCreated(null),
  });

  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api<Tenant>(`/tenants/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "租户状态已更新" });
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const payload = {
      name: form.name.trim(),
      slug: form.slug.trim(),
      owner_email: form.owner_email.trim().toLowerCase(),
      owner_display_name: form.owner_display_name.trim(),
    };
    const invalid = validateTenantForm(payload);
    if (invalid) {
      setMessage({ kind: "error", text: invalid });
      return;
    }
    setCreated(null);
    setMessage(null);
    create.mutate(payload);
  }

  function copyPassword() {
    if (!created) return;
    void navigator.clipboard.writeText(created.owner.initial_password).then(
      () => setMessage({ kind: "ok", text: "已复制临时密码" }),
      () => setMessage({ kind: "error", text: "复制失败，请手动选择" }),
    );
  }

  const currentTenantId = user?.tenant_id ?? "";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">租户</h1>
        <p className="mt-1 text-sm text-ink-500">
          平台级租户管理。创建租户会同时开通所有者账号，并生成一次性临时密码。
        </p>
      </div>

      <form
        className="grid gap-3 card p-5 md:grid-cols-2 lg:grid-cols-4"
        onSubmit={submit}
      >
        <label className="space-y-1 text-sm text-ink-500">
          <span>租户名称 *</span>
          <input
            className="input text-ink-900"
            maxLength={100}
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="例如：客户 A"
          />
        </label>
        <label className="space-y-1 text-sm text-ink-500">
          <span>租户标识 *</span>
          <input
            className="input text-ink-900"
            minLength={2}
            maxLength={50}
            pattern="[a-z0-9][a-z0-9-]{1,49}"
            required
            value={form.slug}
            onChange={(e) => setForm({ ...form, slug: e.target.value })}
            placeholder="customer-a"
          />
        </label>
        <label className="space-y-1 text-sm text-ink-500">
          <span>所有者邮箱 *</span>
          <input
            className="input text-ink-900"
            maxLength={255}
            required
            type="email"
            value={form.owner_email}
            onChange={(e) =>
              setForm({ ...form, owner_email: e.target.value })
            }
            placeholder="owner@example.com"
          />
        </label>
        <label className="space-y-1 text-sm text-ink-500">
          <span>所有者显示名 *</span>
          <input
            className="input text-ink-900"
            maxLength={100}
            required
            value={form.owner_display_name}
            onChange={(e) =>
              setForm({ ...form, owner_display_name: e.target.value })
            }
            placeholder="例如：客户管理员"
          />
        </label>
        <div className="md:col-span-2 lg:col-span-4">
          <button
            className="btn-primary"
            disabled={create.isPending}
          >
            {create.isPending ? "创建中…" : "创建租户"}
          </button>
        </div>
      </form>

      {message && (
        <p
          className={
            message.kind === "error"
              ? "text-sm text-wine-600"
              : "text-sm text-sage-600"
          }
        >
          {message.text}
        </p>
      )}
      {create.isError && (
        <p className="text-sm text-wine-600">{errorText(create.error)}</p>
      )}

      {created && (
        <div className="rounded-xl border border-sage-100 bg-surface p-4 text-sm">
          <p className="text-sage-600">
            已创建租户「{created.name}」，所有者 {created.owner.email}
          </p>
          <code className="mt-2 block break-all rounded bg-paper px-3 py-2 font-mono text-ink-900">
            {created.owner.initial_password}
          </code>
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              className="rounded border border-line px-3 py-1.5 text-xs text-ink-700"
              onClick={copyPassword}
            >
              复制临时密码
            </button>
            <span className="text-xs text-ink-500">
              只显示一次；所有者首次登录后必须修改密码。
            </span>
          </div>
        </div>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">全部租户</h2>
        {tenants.isLoading && (
          <p className="text-sm text-ink-500">加载中…</p>
        )}
        {tenants.isError && (
          <p className="text-sm text-wine-600">{errorText(tenants.error)}</p>
        )}
        {tenants.data?.length === 0 && (
          <p className="text-sm text-ink-500">还没有租户。</p>
        )}
        {tenants.data?.map((tenant) => {
          const pending =
            update.isPending && update.variables?.id === tenant.id;
          return (
            <article
              key={tenant.id}
              className="flex flex-wrap items-center justify-between gap-3 card p-4"
            >
              <div>
                <h3 className="font-medium">
                  {tenant.name}
                  {tenant.id === currentTenantId && (
                    <span className="ml-2 text-xs text-ink-500">（当前）</span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-ink-500">
                  {tenant.slug} · {tenant.members_count} 名成员 · 创建于{" "}
                  {tenant.created_at.slice(0, 10)}
                </p>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <span
                  className={
                    tenant.status === "active"
                      ? "text-xs text-sage-600"
                      : "text-xs text-ink-500"
                  }
                >
                  {tenantStatusLabel(tenant.status)}
                </span>
                {canSuspendTenant(tenant, currentTenantId) && (
                  <button
                    type="button"
                    className="rounded border border-wine-100 px-3 py-1.5 text-wine-600 hover:border-wine-100 disabled:opacity-50"
                    disabled={pending}
                    onClick={() =>
                      update.mutate({ id: tenant.id, status: "suspended" })
                    }
                  >
                    停用
                  </button>
                )}
                {canEnableTenant(tenant) && (
                  <button
                    type="button"
                    className="rounded border border-line px-3 py-1.5 text-ink-700 hover:border-ink-300 disabled:opacity-50"
                    disabled={pending}
                    onClick={() =>
                      update.mutate({ id: tenant.id, status: "active" })
                    }
                  >
                    启用
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
