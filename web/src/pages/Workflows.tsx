import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText } from "../types";
import type {
  TemplateSummary,
  WorkflowSummary,
} from "../types/workflow";
import { canEditWorkflow, runStatusLabel } from "../workflows";

function timestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export default function Workflows() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const editable = me ? canEditWorkflow(me.role) : false;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templateKey, setTemplateKey] = useState("");
  const [templateName, setTemplateName] = useState("");
  const [error, setError] = useState("");

  const workflows = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api<WorkflowSummary[]>("/workflows"),
  });

  const templates = useQuery({
    queryKey: ["workflow-templates"],
    queryFn: () => api<TemplateSummary[]>("/workflows/templates"),
    enabled: editable,
  });

  const create = useMutation({
    mutationFn: () =>
      api<WorkflowSummary>("/workflows", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), description: description.trim() }),
      }),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${workflow.id}`);
    },
    onError: (err) => setError(errorText(err)),
  });

  const instantiate = useMutation({
    mutationFn: () =>
      api<WorkflowSummary>(`/workflows/templates/${templateKey}/instantiate`, {
        method: "POST",
        body: JSON.stringify({ name: templateName.trim() }),
      }),
    onSuccess: (workflow) => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${workflow.id}`);
    },
    onError: (err) => setError(errorText(err)),
  });

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("请填写工作流名称");
      return;
    }
    setError("");
    create.mutate();
  }

  function submitTemplate(event: FormEvent) {
    event.preventDefault();
    if (!templateKey) {
      setError("请选择模板");
      return;
    }
    if (!templateName.trim()) {
      setError("请填写新工作流名称");
      return;
    }
    setError("");
    instantiate.mutate();
  }

  const selected = templates.data?.find((item) => item.key === templateKey) ?? null;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">工作流</h1>
        <p className="mt-1 text-sm text-ink-500">
          用画布编排输入、工具、Agent、条件与审核节点，发布后手动或定时运行。
        </p>
      </header>

      {error && <p className="text-sm text-wine-700">{error}</p>}
      {workflows.isError && (
        <p className="text-sm text-wine-700">{errorText(workflows.error)}</p>
      )}

      <section className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-paper text-left text-xs text-ink-500">
            <tr>
              <th className="px-4 py-3 font-medium">名称</th>
              <th className="px-4 py-3 font-medium">版本</th>
              <th className="px-4 py-3 font-medium">下次定时</th>
              <th className="px-4 py-3 font-medium">最近运行</th>
              <th className="px-4 py-3 font-medium">创建人</th>
            </tr>
          </thead>
          <tbody>
            {workflows.data?.length === 0 && (
              <tr>
                <td className="px-4 py-6 text-ink-500" colSpan={5}>
                  还没有工作流。
                </td>
              </tr>
            )}
            {workflows.data?.map((workflow) => (
              <tr key={workflow.id} className="border-t border-line">
                <td className="px-4 py-3">
                  <Link className="font-medium text-sage-700" to={`/workflows/${workflow.id}`}>
                    {workflow.name}
                  </Link>
                  {workflow.has_unpublished && (
                    <span className="badge ml-2 bg-wine-50 text-wine-700">有未发布修改</span>
                  )}
                  {workflow.description && (
                    <p className="mt-1 text-xs text-ink-500">{workflow.description}</p>
                  )}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.latest_version == null ? "未发布" : `v${workflow.latest_version}`}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.next_run_at ? timestamp(workflow.next_run_at) : "—"}
                </td>
                <td className="px-4 py-3 text-ink-700">
                  {workflow.last_run ? (
                    <span>
                      {runStatusLabel(workflow.last_run.status)}
                      <span className="ml-2 text-xs text-ink-500">
                        {timestamp(workflow.last_run.created_at)}
                      </span>
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-4 py-3 text-ink-500">{workflow.created_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {editable && (
        <div className="grid gap-6 lg:grid-cols-2">
          <form className="card card-pad space-y-3" onSubmit={submitCreate}>
            <h2 className="text-sm font-semibold">新建工作流</h2>
            <label className="space-y-1 text-sm text-ink-500">
              <span>名称 *</span>
              <input
                className="input"
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：每周选题创作"
              />
            </label>
            <label className="space-y-1 text-sm text-ink-500">
              <span>说明</span>
              <input
                className="input"
                maxLength={2000}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <button className="btn-primary" disabled={create.isPending} type="submit">
              {create.isPending ? "创建中…" : "创建草稿"}
            </button>
          </form>

          <form className="card card-pad space-y-3" onSubmit={submitTemplate}>
            <h2 className="text-sm font-semibold">从模板创建</h2>
            <label className="space-y-1 text-sm text-ink-500">
              <span>模板</span>
              <select
                className="input"
                value={templateKey}
                onChange={(event) => {
                  setTemplateKey(event.target.value);
                  const item = templates.data?.find(
                    (template) => template.key === event.target.value,
                  );
                  setTemplateName(item?.name ?? "");
                }}
              >
                <option value="">选择模板</option>
                {(templates.data ?? []).map((template) => (
                  <option key={template.key} value={template.key}>
                    {template.name}
                  </option>
                ))}
              </select>
            </label>
            {selected && (
              <p className="text-xs text-ink-500">{selected.description}</p>
            )}
            <label className="space-y-1 text-sm text-ink-500">
              <span>新工作流名称 *</span>
              <input
                className="input"
                maxLength={120}
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
              />
            </label>
            <button
              className="btn-secondary"
              disabled={instantiate.isPending || !templateKey}
              type="submit"
            >
              {instantiate.isPending ? "创建中…" : "实例化模板"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
