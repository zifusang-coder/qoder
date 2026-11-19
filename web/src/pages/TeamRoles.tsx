import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { canEditTeamRoles, type TeamRoleConfig } from "../agentTeam";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";

const LOCKED_ROLES = ["planner", "creator"];

export default function TeamRoles() {
  const { user: me } = useAuth();
  const queryClient = useQueryClient();
  const rolesQuery = useQuery({
    queryKey: ["team-roles"],
    queryFn: () => api<TeamRoleConfig[]>("/agent/team/roles"),
  });
  const [draft, setDraft] = useState<TeamRoleConfig[] | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (rolesQuery.data && draft === null) setDraft(rolesQuery.data);
  }, [rolesQuery.data, draft]);

  const save = useMutation({
    mutationFn: () =>
      api<TeamRoleConfig[]>("/agent/team/roles", {
        method: "PUT",
        body: JSON.stringify({
          roles: (draft ?? []).map(({ role, enabled, tools, budget }) => ({
            role,
            enabled,
            tools,
            budget,
          })),
        }),
      }),
    onSuccess: (data) => {
      setDraft(data);
      setError("");
      setMessage("已保存");
      void queryClient.invalidateQueries({ queryKey: ["team-roles"] });
    },
    onError: (err) => {
      setMessage("");
      setError(errorText(err));
    },
  });

  if (!me || !canEditTeamRoles(me.role)) {
    return (
      <p className="text-sm text-wine-700">无权限：仅管理员可配置协作团队角色。</p>
    );
  }

  function patchRole(role: string, patch: Partial<TeamRoleConfig>) {
    setDraft((current) =>
      current
        ? current.map((item) => (item.role === role ? { ...item, ...patch } : item))
        : current,
    );
  }

  return (
    <section className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">协作团队角色配置</h1>
        <Link className="text-sm text-sage-700" to="/settings">
          返回设置
        </Link>
      </div>
      <p className="text-sm text-ink-500">
        租户只能收窄角色工具、调低预算；Lead 规划与创作角色固定启用。
      </p>
      {rolesQuery.isLoading && <p className="text-sm text-ink-500">加载中…</p>}
      {rolesQuery.isError && (
        <p className="text-sm text-wine-600">{errorText(rolesQuery.error)}</p>
      )}
      {draft?.map((config) => {
        const locked = LOCKED_ROLES.includes(config.role);
        return (
          <div key={config.role} className="card p-6 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="font-medium">
                {config.label}
                <span className="ml-2 text-xs text-ink-500">{config.role}</span>
              </h2>
              <label className="flex items-center gap-2 text-sm text-ink-700">
                <input
                  type="checkbox"
                  checked={config.enabled}
                  disabled={locked}
                  onChange={(event) =>
                    patchRole(config.role, { enabled: event.target.checked })
                  }
                />
                启用
                {locked && <span className="text-xs text-ink-500">（固定启用）</span>}
              </label>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-ink-500">工具（仅可取消，不能新增）</p>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {config.default_tools.map((tool) => (
                  <label
                    key={tool}
                    className="flex items-center gap-1 text-xs text-ink-700"
                  >
                    <input
                      type="checkbox"
                      checked={config.tools.includes(tool)}
                      onChange={(event) =>
                        patchRole(config.role, {
                          tools: event.target.checked
                            ? [...config.tools, tool]
                            : config.tools.filter((item) => item !== tool),
                        })
                      }
                    />
                    {tool}
                  </label>
                ))}
              </div>
            </div>
            <div className="space-y-1">
              <p className="text-xs text-ink-500">留空保存后恢复默认上限</p>
              <div className="grid grid-cols-2 gap-2">
                {Object.keys(config.limits).map((key) => (
                  <label key={key} className="block space-y-1 text-xs text-ink-500">
                    <span>
                      {key}（上限 {config.limits[key]}）
                    </span>
                    <input
                      className="input text-xs text-ink-900"
                      type="number"
                      min={1}
                      max={config.limits[key]}
                      placeholder={String(config.limits[key])}
                      value={config.budget[key] ?? ""}
                      onChange={(event) => {
                        const raw = event.target.value;
                        const next = { ...config.budget };
                        if (raw === "") {
                          delete next[key];
                        } else {
                          next[key] = Number(raw);
                        }
                        patchRole(config.role, { budget: next });
                      }}
                    />
                  </label>
                ))}
              </div>
            </div>
          </div>
        );
      })}
      {error && <p className="text-sm text-wine-600">{error}</p>}
      {message && !error && <p className="text-sm text-sage-600">{message}</p>}
      <button
        className="btn-primary"
        disabled={!draft || save.isPending}
        type="button"
        onClick={() => {
          setMessage("");
          setError("");
          save.mutate();
        }}
      >
        {save.isPending ? "保存中…" : "保存"}
      </button>
    </section>
  );
}