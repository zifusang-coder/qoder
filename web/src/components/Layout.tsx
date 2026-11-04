import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { setToken } from "../client";

const NAV_ITEMS = [
  { to: "/chat", label: "助手" },
  { to: "/profiles", label: "账号画像" },
  { to: "/projects", label: "项目" },
  { to: "/accounts", label: "发布账号" },
  { to: "/calendar", label: "日历" },
  { to: "/publish", label: "发布" },
  { to: "/analytics", label: "数据" },
  { to: "/settings", label: "设置" },
];

export default function Layout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  function logout() {
    // Drop cached data from the previous session so the next login starts clean.
    queryClient.clear();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-900">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-3 px-6 py-4">
          <span className="text-sm font-semibold tracking-wide text-indigo-400">
            新媒体运营
          </span>
          <nav className="flex flex-1 flex-wrap gap-1 text-sm">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `rounded px-3 py-1.5 transition-colors ${
                    isActive
                      ? "bg-slate-800 text-indigo-400"
                      : "text-slate-400 hover:text-slate-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <button
            type="button"
            className="text-sm text-slate-400 hover:text-slate-200"
            onClick={logout}
          >
            退出登录
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
