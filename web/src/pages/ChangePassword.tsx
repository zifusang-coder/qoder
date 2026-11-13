import { useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth-context";
import { validatePasswordChange } from "../changePassword";
import { api, setToken } from "../client";
import { errorText } from "../types";

export default function ChangePassword() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const invalid = validatePasswordChange(current, next, confirm);
    if (invalid) {
      setError(invalid);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await api<{ access_token: string }>(
        "/auth/change-password",
        {
          method: "POST",
          body: JSON.stringify({
            current_password: current,
            new_password: next,
          }),
        },
      );
      setToken(data.access_token);
      // 新 token 的 ver 已递增；刷新 me 后再跳转，避免被强制改密重定向回来。
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      navigate("/", { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    queryClient.clear();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <main className="mx-auto mt-24 w-full max-w-md card p-8">
      <h1 className="text-xl font-semibold">修改密码</h1>
      <p className="mt-1 text-sm text-ink-500">
        {user?.must_change_password
          ? "首次登录或密码被重置后，需要先设置新密码。"
          : "修改后其他设备上的登录状态会立即失效。"}
      </p>
      <form className="mt-6 space-y-3" onSubmit={submit}>
        <input
          className="input"
          aria-label="当前密码"
          required
          type="password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          placeholder="当前密码"
        />
        <input
          className="input"
          aria-label="新密码"
          required
          minLength={8}
          maxLength={128}
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          placeholder="新密码（8-128 位）"
        />
        <input
          className="input"
          aria-label="确认新密码"
          required
          minLength={8}
          maxLength={128}
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="再次输入新密码"
        />
        {error && <p className="text-sm text-wine-600">{error}</p>}
        <button
          className="btn-primary w-full"
          disabled={loading}
        >
          {loading ? "保存中…" : "保存新密码"}
        </button>
      </form>
      <button
        type="button"
        className="mt-4 text-sm text-ink-500 hover:text-ink-900"
        onClick={logout}
      >
        退出登录
      </button>
    </main>
  );
}
