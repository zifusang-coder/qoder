import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../client";

type Provider = {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  api_key_masked: string;
  tiers: Record<string, string>;
  is_default: boolean;
};

function errorMessage(err: unknown): string {
  if (err instanceof ApiError && Array.isArray(err.detail)) {
    const messages = err.detail
      .map((item) =>
        item && typeof item === "object" && "msg" in item
          ? (item as { msg?: unknown }).msg
          : undefined,
      )
      .filter((msg): msg is string => typeof msg === "string" && msg.trim() !== "");
    if (messages.length > 0) return messages.join("；");
  }
  if (err instanceof Error && err.message) return err.message;
  return "保存失败";
}

export default function Settings() {
  const qc = useQueryClient();
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<Provider[]>("/settings/providers"),
  });
  const [form, setForm] = useState({
    name: "OpenAI 兼容",
    kind: "openai_compatible",
    base_url: "https://api.openai.com/v1",
    api_key: "",
    fast: "gpt-4o-mini",
    standard: "gpt-4o",
    premium: "gpt-4o",
    is_default: true,
  });
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"ok" | "error">("ok");

  const create = useMutation({
    mutationFn: () => {
      const tiers = Object.fromEntries(
        Object.entries({
          fast: form.fast.trim(),
          standard: form.standard.trim(),
          premium: form.premium.trim(),
        }).filter(([, value]) => value !== ""),
      );
      return api<Provider>("/settings/providers", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          kind: form.kind,
          base_url: form.base_url,
          api_key: form.api_key,
          tiers,
          is_default: form.is_default,
        }),
      });
    },
    onSuccess: () => {
      setMessageKind("ok");
      setMessage("已保存");
      setForm((f) => ({ ...f, api_key: "" }));
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
    onError: (err) => {
      setMessageKind("error");
      setMessage(errorMessage(err));
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!form.standard.trim()) {
      setMessageKind("error");
      setMessage("请至少填写 standard 模型");
      return;
    }
    if (!/^[\x20-\x7E]+$/.test(form.api_key)) {
      setMessageKind("error");
      setMessage("API Key 只能包含 ASCII 字符");
      return;
    }
    setMessage("");
    create.mutate();
  }

  return (
    <section className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">系统设置</h1>
        <Link className="text-sm text-sage-700" to="/chat">
          返回对话
        </Link>
      </div>
      <form className="space-y-3 card p-6" onSubmit={submit}>
        <h2 className="font-medium">添加 LLM 提供商</h2>
        <div className="space-y-1">
          <label className="block text-sm text-ink-500" htmlFor="name">
            名称
          </label>
          <input
            id="name"
            className="input"
            maxLength={100}
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="名称"
          />
        </div>
        <div className="space-y-1">
          <label className="block text-sm text-ink-500" htmlFor="kind">
            类型
          </label>
          <select
            id="kind"
            className="input"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            <option value="openai_compatible">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="block text-sm text-ink-500" htmlFor="base_url">
            Base URL
          </label>
          <input
            id="base_url"
            className="input"
            maxLength={500}
            required
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            placeholder="Base URL"
          />
        </div>
        <div className="space-y-1">
          <label className="block text-sm text-ink-500" htmlFor="api_key">
            API Key
          </label>
          <input
            id="api_key"
            className="input"
            type="password"
            minLength={8}
            maxLength={512}
            required
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            placeholder="API Key"
          />
        </div>
        <div className="grid grid-cols-3 gap-2">
          <div className="space-y-1">
            <label className="block text-sm text-ink-500" htmlFor="fast">
              fast 模型
            </label>
            <input
              id="fast"
              className="input"
              value={form.fast}
              onChange={(e) => setForm({ ...form, fast: e.target.value })}
              placeholder="fast 模型"
            />
          </div>
          <div className="space-y-1">
            <label className="block text-sm text-ink-500" htmlFor="standard">
              standard 模型
            </label>
            <input
              id="standard"
              className="input"
              required
              value={form.standard}
              onChange={(e) => setForm({ ...form, standard: e.target.value })}
              placeholder="standard 模型"
            />
          </div>
          <div className="space-y-1">
            <label className="block text-sm text-ink-500" htmlFor="premium">
              premium 模型
            </label>
            <input
              id="premium"
              className="input"
              value={form.premium}
              onChange={(e) => setForm({ ...form, premium: e.target.value })}
              placeholder="premium 模型"
            />
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-700" htmlFor="is_default">
          <input
            id="is_default"
            type="checkbox"
            checked={form.is_default}
            onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
          />
          设为默认提供商
        </label>
        {message && (
          <p className={messageKind === "ok" ? "text-sm text-sage-600" : "text-sm text-wine-600"}>
            {message}
          </p>
        )}
        <button
          className="btn-primary"
          disabled={create.isPending}
        >
          {create.isPending ? "保存中…" : "保存"}
        </button>
      </form>
      <section className="space-y-2">
        <h2 className="font-medium">已配置</h2>
        {providers.isLoading && <p className="text-sm text-ink-500">加载中…</p>}
        {providers.isError && (
          <p className="text-sm text-wine-600">
            {providers.error instanceof Error ? providers.error.message : "加载失败"}
          </p>
        )}
        {providers.data?.length === 0 && <p className="text-sm text-ink-500">暂无</p>}
        {providers.data?.map((p) => (
          <div key={p.id} className="rounded border border-line bg-surface p-3 text-sm">
            <span className="font-medium">{p.name}</span>
            <span className="ml-2 text-ink-500">{p.base_url}</span>
            <span className="ml-2 font-mono text-ink-500">{p.api_key_masked}</span>
            {p.is_default && <span className="ml-2 text-sage-600">默认</span>}
          </div>
        ))}
      </section>
    </section>
  );
}
