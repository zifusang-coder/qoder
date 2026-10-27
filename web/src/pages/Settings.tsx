import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../client";

type Provider = {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  api_key_masked: string;
  tiers: Record<string, string>;
  is_default: boolean;
};

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
          is_default: true,
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
      setMessage(err instanceof Error ? err.message : "保存失败");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!form.standard.trim()) {
      setMessageKind("error");
      setMessage("请至少填写 standard 模型");
      return;
    }
    setMessage("");
    create.mutate();
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">系统设置</h1>
        <Link className="text-sm text-indigo-400" to="/chat">
          返回对话
        </Link>
      </div>
      <form className="space-y-3 rounded-xl border border-slate-800 bg-slate-900 p-6" onSubmit={submit}>
        <h2 className="font-medium">添加 LLM 提供商</h2>
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          maxLength={100}
          required
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="名称"
        />
        <select
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          value={form.kind}
          onChange={(e) => setForm({ ...form, kind: e.target.value })}
        >
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
        </select>
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          maxLength={500}
          required
          value={form.base_url}
          onChange={(e) => setForm({ ...form, base_url: e.target.value })}
          placeholder="Base URL"
        />
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          type="password"
          minLength={8}
          maxLength={512}
          required
          value={form.api_key}
          onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          placeholder="API Key"
        />
        <div className="grid grid-cols-3 gap-2">
          <input
            className="rounded border border-slate-700 bg-slate-950 px-3 py-2"
            value={form.fast}
            onChange={(e) => setForm({ ...form, fast: e.target.value })}
            placeholder="fast 模型"
          />
          <input
            className="rounded border border-slate-700 bg-slate-950 px-3 py-2"
            required
            value={form.standard}
            onChange={(e) => setForm({ ...form, standard: e.target.value })}
            placeholder="standard 模型"
          />
          <input
            className="rounded border border-slate-700 bg-slate-950 px-3 py-2"
            value={form.premium}
            onChange={(e) => setForm({ ...form, premium: e.target.value })}
            placeholder="premium 模型"
          />
        </div>
        {message && (
          <p className={messageKind === "ok" ? "text-sm text-emerald-400" : "text-sm text-red-400"}>
            {message}
          </p>
        )}
        <button
          className="rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
          disabled={create.isPending}
        >
          {create.isPending ? "保存中…" : "保存"}
        </button>
      </form>
      <section className="space-y-2">
        <h2 className="font-medium">已配置</h2>
        {providers.isLoading && <p className="text-sm text-slate-400">加载中…</p>}
        {providers.data?.length === 0 && <p className="text-sm text-slate-400">暂无</p>}
        {providers.data?.map((p) => (
          <div key={p.id} className="rounded border border-slate-800 bg-slate-900 p-3 text-sm">
            <span className="font-medium">{p.name}</span>
            <span className="ml-2 text-slate-400">{p.base_url}</span>
            <span className="ml-2 font-mono text-slate-500">{p.api_key_masked}</span>
            {p.is_default && <span className="ml-2 text-emerald-400">默认</span>}
          </div>
        ))}
      </section>
    </main>
  );
}
