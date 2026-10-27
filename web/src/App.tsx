import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { ApiError, api, getToken } from "./client";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Settings from "./pages/Settings";

function ProtectedLayout() {
  const token = getToken();
  const me = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => api<unknown>("/auth/me"),
    enabled: Boolean(token),
    retry: false,
  });

  if (!token) return <Navigate to="/login" replace />;
  if (me.isLoading) {
    return <p className="p-8 text-sm text-slate-400">正在校验登录状态…</p>;
  }
  // Only an explicit 401 logs the user out; network/5xx errors render the
  // protected page so a backend hiccup does not kick the user to /login.
  if (me.error instanceof ApiError && me.error.status === 401) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedLayout />}>
        <Route path="/settings" element={<Settings />} />
        <Route path="/chat" element={<Chat />} />
      </Route>
      <Route path="*" element={<Navigate to={getToken() ? "/chat" : "/login"} replace />} />
    </Routes>
  );
}
