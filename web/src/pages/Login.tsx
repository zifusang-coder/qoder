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
    <main className="mx-auto mt-20 w-full max-w-sm px-6">
      <div className="card p-8">
        <span className="brand-mark h-11 w-11 rounded-xl text-base">云</span>
        <h1 className="mt-4 text-xl font-semibold text-ink-900">{APP_NAME}</h1>
        <p className="mt-1 text-sm text-ink-500">登录后开始创作与发布</p>
        <form className="mt-6 space-y-3" onSubmit={submit}>
          <input
            className="input"
            aria-label="邮箱"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="邮箱"
          />
          <input
            className="input"
            aria-label="密码"
            required
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="密码"
          />
          {error && <p className="text-sm text-coral-600">{error}</p>}
          <button className="btn-primary w-full" disabled={loading}>
            {loading ? "登录中…" : "登录"}
          </button>
        </form>
      </div>
    </main>
  );
}
