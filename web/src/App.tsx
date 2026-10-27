import { useEffect } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, getToken } from "./client";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Settings from "./pages/Settings";

export default function App() {
  const navigate = useNavigate();

  useEffect(() => {
    if (!getToken()) return;
    api("/auth/me").catch(() => navigate("/login"));
  }, [navigate]);

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/chat" element={<Chat />} />
      <Route path="*" element={<Navigate to={getToken() ? "/chat" : "/login"} replace />} />
    </Routes>
  );
}
