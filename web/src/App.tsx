import { Navigate, Route, Routes } from "react-router-dom";
import { defaultPath } from "./auth";
import { ProtectedLayout, RequireRole, useAuth } from "./auth-context";
import { getToken } from "./client";
import Layout from "./components/Layout";
import Accounts from "./pages/Accounts";
import Analytics from "./pages/Analytics";
import Calendar from "./pages/Calendar";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Profiles from "./pages/Profiles";
import ProjectDetail from "./pages/ProjectDetail";
import Projects from "./pages/Projects";
import Publish from "./pages/Publish";
import Settings from "./pages/Settings";

function HomeRedirect() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  return <Navigate to={defaultPath(user)} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedLayout />}>
        <Route element={<Layout />}>
          <Route index element={<HomeRedirect />} />
          <Route
            path="/chat"
            element={
              <RequireRole minRole="editor">
                <Chat />
              </RequireRole>
            }
          />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/publish" element={<Publish />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route
            path="/settings"
            element={
              <RequireRole minRole="admin">
                <Settings />
              </RequireRole>
            }
          />
        </Route>
      </Route>
      <Route
        path="*"
        element={<Navigate to={getToken() ? "/" : "/login"} replace />}
      />
    </Routes>
  );
}
