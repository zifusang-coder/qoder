import { useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { visibleNavItems } from "../auth";
import { useAuth } from "../auth-context";
import { APP_NAME } from "../branding";
import { setToken } from "../client";
import { groupedNav } from "../nav";
import NavIcon from "./NavIcon";
import NotificationBell from "./NotificationBell";

export default function Layout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const navItems = user ? visibleNavItems(user) : [];
  const groups = groupedNav(navItems);

  function logout() {
    // Drop cached data from the previous session so the next login starts clean.
    queryClient.clear();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen md:pl-20">
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-20 flex-col border-r border-white/70 bg-white/80 backdrop-blur-md md:flex">
        <Link
          to="/"
          className="flex flex-col items-center gap-1 px-2 pb-2 pt-4"
          title={APP_NAME}
        >
          <span className="brand-mark">云</span>
        </Link>
        <nav className="flex-1 overflow-y-auto px-2 py-2">
          {groups.map((group, index) => (
            <div
              key={group.key}
              className={index > 0 ? "mt-2 border-t border-line/70 pt-2" : ""}
            >
              <p className="mb-1 text-center text-[10px] tracking-[0.2em] text-ink-300">
                {group.label}
              </p>
              <div className="space-y-1">
                {group.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    title={item.label}
                    className={({ isActive }) =>
                      `flex flex-col items-center gap-1 rounded-lg px-1 py-1.5 text-[11px] leading-none transition-colors ${
                        isActive
                          ? "bg-brand-50 font-medium text-brand-700"
                          : "text-ink-500 hover:bg-brand-50/60 hover:text-brand-700"
                      }`
                    }
                  >
                    <NavIcon name={item.icon} className="h-5 w-5" />
                    <span className="max-w-full truncate">{item.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
      </aside>

      <header className="sticky top-0 z-30 border-b border-white/70 bg-white/75 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-3 px-6 py-3.5">
          <Link className="flex items-center gap-2 md:hidden" to="/">
            <span className="brand-mark">云</span>
            <span className="text-sm font-semibold tracking-wide text-brand-700">
              {APP_NAME}
            </span>
          </Link>
          <span className="hidden text-sm font-medium tracking-wide text-brand-700 md:inline">
            {APP_NAME}
          </span>
          <span className="text-xs text-ink-500">{user?.tenant_name}</span>
          <nav className="flex flex-1 flex-wrap gap-1 md:hidden">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  isActive ? "nav-link-active" : "nav-link"
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <span className="ml-auto flex items-center gap-1">
            <NotificationBell />
            <Link className="nav-link" to="/change-password">
              修改密码
            </Link>
            <button type="button" className="nav-link" onClick={logout}>
              退出登录
            </button>
          </span>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
