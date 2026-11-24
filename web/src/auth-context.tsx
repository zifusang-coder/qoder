import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { ApiError, api, getToken, setToken } from "./client";
import {
  atLeast,
  canWrite,
  isAdmin,
  isOwner,
  type Me,
  type Role,
} from "./auth";

interface AuthState {
  user: Me | null;
  role: Role | null;
  loading: boolean;
  error: unknown;
  canWrite: boolean;
  isAdmin: boolean;
  isOwner: boolean;
  isPlatformAdmin: boolean;
  refresh: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const token = getToken();
  const query = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => api<Me>("/auth/me"),
    enabled: Boolean(token),
    retry: false,
  });
  const user = query.data ?? null;
  return (
    <AuthContext.Provider
      value={{
        user,
        role: user?.role ?? null,
        loading: Boolean(token) && query.isLoading,
        error: query.error,
        canWrite: user ? canWrite(user.role) : false,
        isAdmin: user ? isAdmin(user.role) : false,
        isOwner: user ? isOwner(user.role) : false,
        isPlatformAdmin: Boolean(user?.is_platform_admin),
        refresh: () => {
          void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
        },
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return value;
}

export function RequireRole({
  minRole,
  platformAdmin = false,
  children,
}: {
  minRole?: Role;
  platformAdmin?: boolean;
  children: ReactNode;
}) {
  const { role, isPlatformAdmin } = useAuth();
  const roleOk =
    minRole === undefined || (role !== null && atLeast(role, minRole));
  if (!roleOk || (platformAdmin && !isPlatformAdmin)) {
    return <p className="text-sm text-coral-600">权限不足，请联系租户管理员。</p>;
  }
  return <>{children}</>;
}

export function ProtectedLayout() {
  const location = useLocation();
  const { user, loading, error } = useAuth();
  const token = getToken();

  if (!token) return <Navigate to="/login" replace />;
  if (loading) {
    return <p className="p-8 text-sm text-ink-500">正在校验登录状态…</p>;
  }
  if (
    error instanceof ApiError &&
    (error.code === "tenant_suspended" || error.code === "account_disabled")
  ) {
    return (
      <main className="mx-auto mt-24 max-w-md rounded-xl border border-coral-100 bg-coral-50 p-8">
        <h1 className="text-lg font-semibold text-coral-700">无法继续访问</h1>
        <p className="mt-2 text-sm text-ink-700">{error.message}</p>
        <button
          type="button"
          className="btn-primary mt-4"
          onClick={() => {
            setToken(null);
            window.location.assign("/login");
          }}
        >
          重新登录
        </button>
      </main>
    );
  }
  // Only an explicit 401 logs the user out; network/5xx errors render the
  // protected page so a backend hiccup does not kick the user to /login.
  if (error instanceof ApiError && error.status === 401) {
    return <Navigate to="/login" replace />;
  }
  if (user) {
    const forced =
      user.must_change_password && location.pathname !== "/change-password";
    if (forced) return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}
