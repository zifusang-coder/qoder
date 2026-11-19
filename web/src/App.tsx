import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { defaultPath } from "./auth";
import { ProtectedLayout, RequireRole, useAuth } from "./auth-context";
import { getToken } from "./client";
import Layout from "./components/Layout";
import Accounts from "./pages/Accounts";
import Analytics from "./pages/Analytics";
import Assistant from "./pages/Assistant";
import Audit from "./pages/Audit";
import Calendar from "./pages/Calendar";
import ChangePassword from "./pages/ChangePassword";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Profiles from "./pages/Profiles";
import ProjectDetail from "./pages/ProjectDetail";
import Projects from "./pages/Projects";
import Publish from "./pages/Publish";
import Settings from "./pages/Settings";
import Team from "./pages/Team";
import TeamRunDetailPage from "./pages/TeamRunDetail";
import Tenants from "./pages/Tenants";
import Workflows from "./pages/Workflows";

const WorkflowEditor = lazy(() => import("./pages/WorkflowEditor"));
const WorkflowRunDetail = lazy(() => import("./pages/WorkflowRunDetail"));

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
        <Route path="/change-password" element={<ChangePassword />} />
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
          <Route
            path="/assistant"
            element={
              <RequireRole minRole="editor">
                <Assistant />
              </RequireRole>
            }
          />
          <Route
            path="/assistant/teams/:id"
            element={
              <RequireRole minRole="editor">
                <TeamRunDetailPage />
              </RequireRole>
            }
          />
          <Route
            path="/workflows"
            element={
              <RequireRole minRole="editor">
                <Workflows />
              </RequireRole>
            }
          />
          <Route
            path="/workflows/:id"
            element={
              <RequireRole minRole="editor">
                <Suspense
                  fallback={
                    <p className="p-6 text-center text-sm text-ink-500">加载中…</p>
                  }
                >
                  <WorkflowEditor />
                </Suspense>
              </RequireRole>
            }
          />
          <Route
            path="/workflows/:id/runs/:runId"
            element={
              <RequireRole minRole="editor">
                <Suspense fallback={<p className="text-sm text-ink-500">加载运行…</p>}>
                  <WorkflowRunDetail />
                </Suspense>
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
          <Route
            path="/team"
            element={
              <RequireRole minRole="admin">
                <Team />
              </RequireRole>
            }
          />
          <Route
            path="/audit"
            element={
              <RequireRole minRole="admin">
                <Audit />
              </RequireRole>
            }
          />
          <Route
            path="/tenants"
            element={
              <RequireRole platformAdmin>
                <Tenants />
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
