import { useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { visibleNavItems } from "../auth";
import { useAuth } from "../auth-context";
import { APP_NAME } from "../branding";
import { setToken } from "../client";

export default function Layout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const navItems = user ? visibleNavItems(user) : [];

  function logout() {
    // Drop cached data from the previous session so the next login starts clean.
    queryClient.clear();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-white/70 bg-white/75 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-3 px-6 py-3.5">
          <Link className="flex items-center gap-2" to="/">
            <span className="brand-mark">云</span>
            <span className="text-sm font-semibold tracking-wide text-brand-700">
              {APP_NAME}
            </span>
          </Link>
          <span className="text-xs text-ink-500">{user?.tenant_name}</span>
          <nav className="flex flex-1 flex-wrap gap-1">
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
          <Link className="nav-link" to="/change-password">
            修改密码
          </Link>
          <button type="button" className="nav-link" onClick={logout}>
            退出登录
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
