import { FormEvent, useState } from "react";
import { APP_NAME } from "../branding";
import { api, setToken } from "../client";

export default function Login() {
  const [email, setEmail] = useState("admin@local");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const data = await api<{ access_token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(data.access_token);
      // AuthProvider snapshots the token on mount; a full reload guarantees it
      // picks up the fresh session before routing to the protected layout.
      window.location.assign("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto mt-24 w-full max-w-sm rounded-xl border border-slate-800 bg-slate-900 p-8">
      <h1 className="text-xl font-semibold">{APP_NAME}</h1>
      <p className="mt-1 text-sm text-slate-400">登录后开始创作与发布</p>
      <form className="mt-6 space-y-3" onSubmit={submit}>
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          aria-label="邮箱"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="邮箱"
        />
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          aria-label="密码"
          required
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="密码"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          className="w-full rounded bg-indigo-600 py-2 font-medium disabled:opacity-50"
          disabled={loading}
        >
          {loading ? "登录中…" : "登录"}
        </button>
      </form>
    </main>
  );
}
