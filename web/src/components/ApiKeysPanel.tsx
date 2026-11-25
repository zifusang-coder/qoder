import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import {
  API_KEY_SCOPES,
  type ApiKey,
  type ApiKeyCreated,
  describeScopes,
  formatKeyTime,
  isKeyActive,
} from "../apiKeys";
import { ApiError, api } from "../client";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError && err.message) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return "操作失败，请稍后重试";
}

export default function ApiKeysPanel() {
  const qc = useQueryClient();
  const keys = useQuery({
    queryKey: ["api-keys"],
    queryFn: () => api<ApiKey[]>("/api-keys"),
  });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["agent:run", "agent:read"]);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api<ApiKeyCreated>("/api-keys", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), scopes }),
      }),
    onSuccess: (data) => {
      setCreated(data);
      setCopied(false);
      setName("");
      setError("");
      qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
    onError: (err) => setError(errorMessage(err)),
  });

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api<ApiKey>(`/api-keys/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setError("");
      qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
    onError: (err) => setError(errorMessage(err)),
  });

  function toggleScope(scope: string) {
    setScopes((current) =>
      current.includes(scope)
        ? current.filter((item) => item !== scope)
        : [...current, scope],
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("请填写密钥名称");
      return;
    }
    if (scopes.length === 0) {
      setError("至少选择一个权限范围");
      return;
    }
    setError("");
    create.mutate();
  }

  async function copySecret() {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.secret);
      setCopied(true);
    } catch {
      setError("复制失败，请手动选择并复制");
    }
  }

  return (
    <section className="space-y-4 card p-6">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-medium">对外 API 密钥</h2>
          <p className="mt-1 text-sm text-ink-500">
            外部脚本通过 X-API-Key 调用 Agent 与工作流。明文只在创建时显示一次，
            之后仅保留前缀用于识别。
          </p>
        </div>
        <span className="badge bg-brand-50 text-brand-700">Public API</span>
      </div>

      {created && (
        <div className="rounded-xl border border-brand-100 bg-brand-50/70 p-3 text-sm">
          <p className="font-medium text-brand-700">
            请立即保存密钥，关闭后无法再次查看
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded-lg bg-white px-3 py-2 font-mono text-xs text-ink-900">
              {created.secret}
            </code>
            <button type="button" className="btn-secondary" onClick={copySecret}>
              {copied ? "已复制" : "复制"}
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setCreated(null)}
            >
              关闭
            </button>
          </div>
        </div>
      )}

      <form className="space-y-3" onSubmit={submit}>
        <div className="space-y-1">
          <label className="block text-sm text-ink-500" htmlFor="api-key-name">
            名称
          </label>
          <input
            id="api-key-name"
            className="input"
            maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如：数据团队周报脚本"
          />
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm text-ink-500">权限范围</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {API_KEY_SCOPES.map((item) => (
              <label
                key={item.value}
                className="flex items-start gap-2 rounded-xl border border-line bg-white px-3 py-2 text-sm"
              >
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={scopes.includes(item.value)}
                  onChange={() => toggleScope(item.value)}
                />
                <span>
                  <span className="block text-ink-900">{item.label}</span>
                  <span className="block text-xs text-ink-500">{item.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        {error && <p className="text-sm text-coral-600">{error}</p>}
        <button className="btn-primary" disabled={create.isPending}>
          {create.isPending ? "创建中…" : "创建密钥"}
        </button>
      </form>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">已签发</h3>
        {keys.isLoading && <p className="text-sm text-ink-500">加载中…</p>}
        {keys.isError && (
          <p className="text-sm text-coral-600">
            {keys.error instanceof Error ? keys.error.message : "加载失败"}
          </p>
        )}
        {keys.data?.length === 0 && (
          <p className="text-sm text-ink-500">
            还没有密钥。外部系统接入时在上方创建。
          </p>
        )}
        {keys.data?.map((key) => {
          const active = isKeyActive(key);
          return (
            <article
              key={key.id}
              className="rounded-xl border border-line bg-surface p-3 text-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{key.name}</span>
                  <span className="font-mono text-xs text-ink-500">
                    {key.prefix}…
                  </span>
                  <span
                    className={
                      active
                        ? "badge bg-mint-50 text-mint-700"
                        : "badge bg-line text-ink-500"
                    }
                  >
                    {active ? "有效" : "已吊销"}
                  </span>
                </div>
                {active && (
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(key.id)}
                  >
                    吊销
                  </button>
                )}
              </div>
              <p className="mt-1 text-xs text-ink-500">
                {describeScopes(key.scopes)} · 创建于{" "}
                {formatKeyTime(key.created_at)} · 最近使用{" "}
                {formatKeyTime(key.last_used_at)}
              </p>
            </article>
          );
        })}
      </div>
    </section>
  );
}
