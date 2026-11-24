import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../auth-context";
import { ApiError, api } from "../client";
import { errorText } from "../types";
import type {
  Cadence,
  Graph,
  InputField,
  SkillList,
  ToolSpec,
  ValidationIssue,
  ValidationResult,
  WorkflowDetail,
  WorkflowRun,
  WorkflowSchedule,
  WorkflowVersionDetail,
  WorkflowVersionSummary,
} from "../types/workflow";
import {
  addNode,
  canEditWorkflow,
  canRunWorkflow,
  connectNodes,
  emptyGraph,
  graphScale,
  inputFields,
  inputPayload,
  moveNode,
  removeNode,
  scheduleText,
  updateNodeData,
  type InputValues,
} from "../workflows";
import NodeInspector from "../components/workflow/NodeInspector";
import NodePalette from "../components/workflow/NodePalette";
import WorkflowCanvas from "../components/workflow/WorkflowCanvas";

const WEEKDAYS = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

interface History {
  past: Graph[];
  future: Graph[];
}

interface ScheduleForm {
  id: string | null;
  kind: "daily" | "weekly";
  weekdays: number[];
  time: string;
  timezone: string;
  values: InputValues;
}

function normalizeGraph(value: Graph | null | undefined): Graph {
  if (!value || !Array.isArray(value.nodes) || !Array.isArray(value.edges)) {
    return emptyGraph();
  }
  return { nodes: value.nodes, edges: value.edges };
}

function issuesFromDetail(detail: unknown): ValidationIssue[] {
  if (typeof detail !== "object" || detail === null) return [];
  const issues = (detail as { issues?: unknown }).issues;
  return Array.isArray(issues) ? (issues as ValidationIssue[]) : [];
}

/** 直接读存储值，不依赖字段清单：编辑表单在固定版本图加载前也要保留原值。 */
function valuesFromSchedule(schedule: WorkflowSchedule): InputValues {
  const values: InputValues = {};
  for (const [key, raw] of Object.entries(schedule.inputs ?? {})) {
    if (typeof raw === "boolean") values[key] = raw;
    else if (raw !== undefined && raw !== null) values[key] = String(raw);
  }
  return values;
}

/** 必填的字符串/数字字段留空时返回其标签；布尔字段 false 视为已填写。 */
function missingRequiredLabels(
  fields: InputField[],
  values: InputValues,
): string[] {
  const missing: string[] = [];
  for (const field of fields) {
    if (!field.required || field.type === "boolean") continue;
    const raw = values[field.key];
    if (raw === undefined || raw === "") missing.push(field.label || field.key);
  }
  return missing;
}

/** 定时固定的是发布版本：新建取最新发布，编辑取定时钉住的版本号。 */
function schedulePinnedVersion(
  form: ScheduleForm | null,
  detail: WorkflowDetail | null,
  versions: WorkflowVersionSummary[] | undefined,
): number | null {
  if (!form) return null;
  if (!form.id) return detail?.latest_published?.version ?? null;
  const schedule = detail?.schedules.find((item) => item.id === form.id) ?? null;
  if (!schedule) return null;
  const pinned = (versions ?? []).find(
    (item) => item.id === schedule.workflow_version_id,
  );
  return pinned?.version ?? null;
}

export default function WorkflowEditor() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user: me } = useAuth();
  const editable = me ? canEditWorkflow(me.role) : false;

  const [graph, setGraph] = useState<Graph>(emptyGraph());
  const [revision, setRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [version, setVersion] = useState(0);
  const [history, setHistory] = useState<History>({ past: [], future: [] });
  const [tab, setTab] = useState<"props" | "schedule">("props");
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [drawer, setDrawer] = useState<"none" | "issues">("none");
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [modalError, setModalError] = useState("");
  const [notice, setNotice] = useState("");
  const [showVersions, setShowVersions] = useState(false);
  const [showPublish, setShowPublish] = useState(false);
  const [showRun, setShowRun] = useState(false);
  const [publishNote, setPublishNote] = useState("");
  const [runValues, setRunValues] = useState<InputValues>({});
  const [scheduleForm, setScheduleForm] = useState<ScheduleForm | null>(null);
  const applied = useRef<number | null>(null);

  const detailQuery = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => api<WorkflowDetail>(`/workflows/${id}`),
    enabled: Boolean(id),
  });
  const detail = detailQuery.data ?? null;

  const toolsQuery = useQuery({
    queryKey: ["agent-tools"],
    queryFn: () => api<ToolSpec[]>("/agent/tools"),
  });
  const skillsQuery = useQuery({
    queryKey: ["skills"],
    queryFn: () => api<SkillList>("/skills"),
  });
  const versionsQuery = useQuery({
    queryKey: ["workflow-versions", id],
    queryFn: () => api<WorkflowVersionSummary[]>(`/workflows/${id}/versions`),
    enabled: Boolean(id) && (showVersions || showPublish || tab === "schedule"),
  });

  const latest = detail?.latest_published ?? null;
  const ready = detailQuery.isSuccess && detail !== null;

  const runVersionNumber = latest?.version ?? null;
  const runVersionQuery = useQuery({
    queryKey: ["workflow-version", id, runVersionNumber],
    queryFn: () =>
      api<WorkflowVersionDetail>(`/workflows/${id}/versions/${runVersionNumber}`),
    enabled: Boolean(id) && showRun && runVersionNumber !== null,
  });

  const pinnedVersion = schedulePinnedVersion(
    scheduleForm,
    detail,
    versionsQuery.data,
  );
  const scheduleVersionQuery = useQuery({
    queryKey: ["workflow-version", id, pinnedVersion],
    queryFn: () =>
      api<WorkflowVersionDetail>(`/workflows/${id}/versions/${pinnedVersion}`),
    enabled: Boolean(id) && pinnedVersion !== null,
  });

  const runFields = runVersionQuery.data
    ? inputFields(normalizeGraph(runVersionQuery.data.graph))
    : [];
  const scheduleFields = scheduleVersionQuery.data
    ? inputFields(normalizeGraph(scheduleVersionQuery.data.graph))
    : [];

  const bump = () => setVersion((value) => value + 1);

  useEffect(() => {
    if (!detail || dirty) return;
    if (applied.current === detail.draft_revision) return;
    applied.current = detail.draft_revision;
    setGraph(normalizeGraph(detail.draft_graph));
    setRevision(detail.draft_revision);
    setSelectedId(null);
    bump();
  }, [detail, dirty]);

  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  function commit(next: Graph) {
    setHistory({
      past: [...history.past, graph].slice(-50),
      future: [],
    });
    setGraph(next);
    setDirty(true);
    bump();
  }

  function undo() {
    if (history.past.length === 0) return;
    const previous = history.past[history.past.length - 1];
    setHistory({
      past: history.past.slice(0, -1),
      future: [graph, ...history.future].slice(0, 50),
    });
    setGraph(previous);
    setDirty(true);
    bump();
  }

  function redo() {
    if (history.future.length === 0) return;
    const next = history.future[0];
    setHistory({
      past: [...history.past, graph].slice(-50),
      future: history.future.slice(1),
    });
    setGraph(next);
    setDirty(true);
    bump();
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.key.toLowerCase() !== "z") return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function leave() {
    if (dirty && !window.confirm("有未保存的修改，确定离开？")) return;
    navigate("/workflows");
  }

  const save = useMutation({
    mutationFn: () =>
      api<WorkflowDetail>(`/workflows/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ revision, draft_graph: graph }),
      }),
    onSuccess: (data) => {
      applied.current = data.draft_revision;
      setRevision(data.draft_revision);
      setGraph(normalizeGraph(data.draft_graph));
      setDirty(false);
      setConflict(false);
      setError("");
      setNotice("草稿已保存");
      bump();
      queryClient.setQueryData(["workflow", id], data);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "workflow_conflict") {
        setConflict(true);
        return;
      }
      setError(errorText(err));
    },
  });

  const validateMutation = useMutation({
    mutationFn: () =>
      api<ValidationResult>(`/workflows/${id}/validate`, { method: "POST" }),
    onSuccess: (result) => {
      setIssues(result.issues);
      setDrawer("issues");
      setNotice(result.ok ? "校验通过" : `发现 ${result.issues.length} 个问题`);
    },
    onError: (err) => setError(errorText(err)),
  });

  const publish = useMutation({
    mutationFn: (input: { revision: number; note: string }) =>
      api<WorkflowVersionSummary>(`/workflows/${id}/publish`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (published) => {
      setShowPublish(false);
      setPublishNote("");
      setModalError("");
      setNotice(`已发布 v${published.version}`);
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflow-versions", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "workflow_invalid") {
        const found = issuesFromDetail(err.detail);
        setIssues(found);
        if (found.length > 0) {
          setDrawer("issues");
          setShowPublish(false);
          setPublishNote("");
          setModalError("");
        } else {
          setModalError(errorText(err));
        }
        return;
      }
      if (err instanceof ApiError && err.code === "workflow_conflict") {
        setConflict(true);
        setShowPublish(false);
        setPublishNote("");
        return;
      }
      setModalError(errorText(err));
    },
  });

  const startRun = useMutation({
    mutationFn: (input: { inputs: Record<string, unknown>; version: number }) =>
      api<WorkflowRun>(`/workflows/${id}/runs`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (run) => {
      setShowRun(false);
      setModalError("");
      navigate(`/workflows/${id}/runs/${run.id}`);
    },
    onError: (err) => setModalError(errorText(err)),
  });

  const restore = useMutation({
    mutationFn: (target: number) =>
      api<WorkflowDetail>(`/workflows/${id}/versions/${target}/restore`, {
        method: "POST",
      }),
    onSuccess: (data) => {
      applied.current = data.draft_revision;
      setRevision(data.draft_revision);
      setGraph(normalizeGraph(data.draft_graph));
      setDirty(false);
      setShowVersions(false);
      setNotice("已恢复到草稿");
      bump();
      queryClient.setQueryData(["workflow", id], data);
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const saveSchedule = useMutation({
    mutationFn: (input: { form: ScheduleForm; fields: InputField[] }) => {
      const { form, fields } = input;
      const cadence: Cadence = {
        kind: form.kind,
        weekdays: form.kind === "weekly" ? form.weekdays.slice().sort() : [],
        time: form.time,
        timezone: form.timezone,
      };
      const body = JSON.stringify({
        cadence,
        inputs: inputPayload(fields, form.values),
      });
      if (form.id) {
        return api<WorkflowSchedule>(`/workflows/${id}/schedules/${form.id}`, {
          method: "PATCH",
          body,
        });
      }
      return api<WorkflowSchedule>(`/workflows/${id}/schedules`, {
        method: "POST",
        body,
      });
    },
    onSuccess: () => {
      setScheduleForm(null);
      setModalError("");
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setModalError(errorText(err)),
  });

  const toggleSchedule = useMutation({
    mutationFn: (input: { id: string; enabled: boolean }) =>
      api<WorkflowSchedule>(`/workflows/${id}/schedules/${input.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: input.enabled }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const deleteSchedule = useMutation({
    mutationFn: (scheduleId: string) =>
      api<void>(`/workflows/${id}/schedules/${scheduleId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflow", id] });
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  async function validateDraft() {
    if (!ready) return;
    setError("");
    setNotice("");
    try {
      await save.mutateAsync();
    } catch {
      return;
    }
    validateMutation.mutate();
  }

  async function confirmPublish() {
    if (!ready) return;
    setError("");
    setModalError("");
    let currentRevision = revision;
    if (dirty) {
      try {
        const saved = await save.mutateAsync();
        currentRevision = saved.draft_revision;
      } catch (err) {
        if (!(err instanceof ApiError && err.code === "workflow_conflict")) {
          setModalError(errorText(err));
        }
        return;
      }
    }
    publish.mutate({ revision: currentRevision, note: publishNote });
  }

  function openPublish() {
    setError("");
    setModalError("");
    setPublishNote("");
    setShowPublish(true);
  }

  function closePublish() {
    setShowPublish(false);
    setPublishNote("");
    setModalError("");
  }

  function openRun() {
    setError("");
    setModalError("");
    setRunValues({});
    setShowRun(true);
  }

  function closeRun() {
    setShowRun(false);
    setModalError("");
  }

  function openSchedule(form: ScheduleForm) {
    setError("");
    setModalError("");
    setScheduleForm(form);
  }

  function closeSchedule() {
    setScheduleForm(null);
    setModalError("");
  }

  function openVersions() {
    setError("");
    setShowVersions(true);
  }

  function submitRun() {
    if (!latest || !runVersionQuery.isSuccess) return;
    const missing = missingRequiredLabels(runFields, runValues);
    if (missing.length > 0) {
      setModalError(`请填写：${missing.join("、")}`);
      return;
    }
    if (dirty && !window.confirm("有未保存的修改，运行将离开当前页面，确定继续？")) {
      return;
    }
    setModalError("");
    startRun.mutate({
      inputs: inputPayload(runFields, runValues),
      version: latest.version,
    });
  }

  function submitSchedule() {
    if (!scheduleForm || !scheduleVersionQuery.isSuccess) return;
    const missing = missingRequiredLabels(scheduleFields, scheduleForm.values);
    if (missing.length > 0) {
      setModalError(`请填写：${missing.join("、")}`);
      return;
    }
    setModalError("");
    saveSchedule.mutate({ form: scheduleForm, fields: scheduleFields });
  }

  function reloadConflict() {
    setConflict(false);
    setDirty(false);
    setError("");
    applied.current = null;
    void detailQuery.refetch();
  }

  const hasInput = graph.nodes.some((node) => node.type === "input");
  const selected = graph.nodes.find((node) => node.id === selectedId) ?? null;
  const runnable = me ? canRunWorkflow(me.role, Boolean(latest)) : false;
  const scale = graphScale(graph);

  return (
    <div className="space-y-4">
      <header className="card card-pad space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-secondary" type="button" onClick={leave}>
            ← 返回
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-lg font-semibold">
              {detail?.name ?? "工作流"}
            </h1>
            <p className="text-xs text-ink-500">
              revision {revision} · {scale.text}
              {scale.level !== "ok" && (
                <span className={scale.level === "over" ? "text-coral-600" : "text-honey-600"}>
                  {" "}
                  · {scale.level === "over" ? "已超过规模上限" : "接近规模上限"}
                </span>
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {editable && (
              <>
                <button
                  className="btn-secondary"
                  disabled={!ready || save.isPending}
                  title={ready ? "" : "加载中…"}
                  type="button"
                  onClick={() => {
                    if (!ready) return;
                    save.mutate();
                  }}
                >
                  {save.isPending ? "保存中…" : dirty ? "保存草稿 *" : "保存草稿"}
                </button>
                <button
                  className="btn-secondary"
                  disabled={!ready || validateMutation.isPending}
                  title={ready ? "" : "加载中…"}
                  type="button"
                  onClick={() => void validateDraft()}
                >
                  校验
                </button>
                <button
                  className="btn-primary"
                  disabled={!ready}
                  title={ready ? "" : "加载中…"}
                  type="button"
                  onClick={openPublish}
                >
                  发布
                </button>
              </>
            )}
            <button
              className="btn-primary"
              disabled={!runnable}
              title={runnable ? "" : "发布后才能运行"}
              type="button"
              onClick={openRun}
            >
              运行{latest ? ` v${latest.version}` : ""}
            </button>
            <button className="btn-secondary" type="button" onClick={openVersions}>
              版本历史
            </button>
          </div>
        </div>
        {(error || detailQuery.isError) && (
          <p className="text-sm text-coral-600">
            {error || errorText(detailQuery.error)}
          </p>
        )}
        {conflict && (
          <div className="flex items-center gap-3 rounded-lg bg-coral-50 px-3 py-2 text-sm text-coral-600">
            <span>草稿已被其他人更新，保存冲突。</span>
            <button className="btn-secondary" type="button" onClick={reloadConflict}>
              重新加载
            </button>
          </div>
        )}
        {notice && !error && <p className="text-sm text-brand-700">{notice}</p>}
      </header>

      <div className="grid gap-4 lg:grid-cols-[160px_minmax(0,1fr)_300px]">
        <NodePalette
          hasInput={hasInput}
          readOnly={!editable}
          onAdd={(type) =>
            commit(
              addNode(graph, type, {
                x: 80 + (graph.nodes.length % 4) * 200,
                y: 80 + Math.floor(graph.nodes.length / 4) * 120,
              }),
            )
          }
        />
        <div className="space-y-2">
          <WorkflowCanvas
            graph={graph}
            version={version}
            readOnly={!editable}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onMoveNode={(nodeId, position) => {
              if (!editable) return;
              commit(moveNode(graph, nodeId, position));
            }}
            onConnectEdge={(connection) => {
              if (!editable || !connection.source || !connection.target) return;
              const result = connectNodes(
                graph,
                connection.source,
                connection.target,
                connection.sourceHandle ?? null,
              );
              if (result.error) {
                setError(result.error);
                return;
              }
              setError("");
              commit(result.graph);
            }}
            onDeleteNodes={(nodeIds) => {
              if (!editable) return;
              let next = graph;
              for (const nodeId of nodeIds) next = removeNode(next, nodeId);
              commit(next);
              if (selectedId && nodeIds.includes(selectedId)) setSelectedId(null);
            }}
            onDeleteEdges={(edgeIds) => {
              if (!editable) return;
              commit({
                ...graph,
                edges: graph.edges.filter((edge) => !edgeIds.includes(edge.id)),
              });
            }}
          />
          {drawer === "issues" && (
            <div className="card card-pad space-y-2">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">校验结果</h2>
                <button
                  className="text-xs text-ink-500"
                  type="button"
                  onClick={() => setDrawer("none")}
                >
                  收起
                </button>
              </div>
              {issues.length === 0 ? (
                <p className="text-sm text-brand-700">没有发现问题。</p>
              ) : (
                <ul className="space-y-1">
                  {issues.map((issue, index) => (
                    <li key={`${issue.code}-${index}`}>
                      <button
                        className="text-left text-sm text-ink-700 hover:text-brand-700"
                        type="button"
                        onClick={() => {
                          if (issue.node_id) {
                            setSelectedId(issue.node_id);
                            setTab("props");
                          }
                          setDrawer("none");
                        }}
                      >
                        <span className="badge mr-2 bg-line text-ink-500">
                          {issue.code}
                        </span>
                        {issue.message}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex gap-1 rounded-lg border border-line bg-surface p-1 text-sm">
            <button
              className={`flex-1 rounded-md px-2 py-1 ${
                tab === "props" ? "bg-brand-50 text-brand-700" : "text-ink-500"
              }`}
              type="button"
              onClick={() => setTab("props")}
            >
              属性
            </button>
            <button
              className={`flex-1 rounded-md px-2 py-1 ${
                tab === "schedule" ? "bg-brand-50 text-brand-700" : "text-ink-500"
              }`}
              type="button"
              onClick={() => setTab("schedule")}
            >
              定时
            </button>
          </div>
          {tab === "props" ? (
            <NodeInspector
              node={selected}
              graph={graph}
              tools={toolsQuery.data ?? []}
              skills={skillsQuery.data?.items ?? []}
              readOnly={!editable}
              onChange={(patch) => {
                if (!selected) return;
                commit(updateNodeData(graph, selected.id, patch));
              }}
              onDelete={() => {
                if (!selected) return;
                commit(removeNode(graph, selected.id));
                setSelectedId(null);
              }}
            />
          ) : (
            <section className="card card-pad space-y-3 text-sm">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold">定时</h2>
                {editable && (
                  <button
                    className="text-xs text-brand-700 disabled:text-ink-300"
                    disabled={!latest}
                    title={latest ? "" : "发布后才能创建定时"}
                    type="button"
                    onClick={() =>
                      openSchedule({
                        id: null,
                        kind: "daily",
                        weekdays: [1],
                        time: "09:00",
                        timezone: "Asia/Shanghai",
                        values: {},
                      })
                    }
                  >
                    + 新建
                  </button>
                )}
              </div>
              {(detail?.schedules ?? []).length === 0 && (
                <p className="text-xs text-ink-500">还没有定时任务。</p>
              )}
              {(detail?.schedules ?? []).map((schedule) => (
                <div key={schedule.id} className="space-y-1 rounded-lg border border-line p-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-ink-900">
                      {scheduleText(schedule)}
                    </span>
                    <label className="flex items-center gap-1 text-[11px] text-ink-500">
                      <input
                        type="checkbox"
                        disabled={!editable || toggleSchedule.isPending}
                        checked={schedule.enabled}
                        onChange={(event) =>
                          toggleSchedule.mutate({
                            id: schedule.id,
                            enabled: event.target.checked,
                          })
                        }
                      />
                      启用
                    </label>
                  </div>
                  <p className="text-[11px] text-ink-500">
                    下次：{schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : "—"}
                  </p>
                  {editable && (
                    <div className="flex gap-3 text-[11px]">
                      <button
                        className="text-brand-700"
                        type="button"
                        onClick={() =>
                          openSchedule({
                            id: schedule.id,
                            kind: schedule.cadence.kind === "weekly" ? "weekly" : "daily",
                            weekdays: schedule.cadence.weekdays?.length
                              ? schedule.cadence.weekdays
                              : [1],
                            time: schedule.cadence.time ?? "09:00",
                            timezone: schedule.cadence.timezone ?? "Asia/Shanghai",
                            values: valuesFromSchedule(schedule),
                          })
                        }
                      >
                        编辑
                      </button>
                      <button
                        className="text-coral-700"
                        type="button"
                        onClick={() => {
                          if (window.confirm("删除这个定时？运行历史会保留。")) {
                            deleteSchedule.mutate(schedule.id);
                          }
                        }}
                      >
                        删除
                      </button>
                    </div>
                  )}
                </div>
              ))}
              {!editable && (
                <p className="text-[11px] text-ink-500">只有 admin/owner 可以修改定时。</p>
              )}
            </section>
          )}
        </div>
      </div>

      {showVersions && (
        <div className="fixed inset-0 z-40 flex justify-end bg-ink-900/20">
          <aside className="h-full w-full max-w-md space-y-3 overflow-y-auto bg-surface p-6">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold">版本历史</h2>
              <button className="text-sm text-ink-500" type="button" onClick={() => setShowVersions(false)}>
                关闭
              </button>
            </div>
            {versionsQuery.isError && (
              <p className="text-sm text-coral-600">{errorText(versionsQuery.error)}</p>
            )}
            {(versionsQuery.data ?? []).length === 0 && (
              <p className="text-sm text-ink-500">还没有发布版本。</p>
            )}
            {(versionsQuery.data ?? []).map((item) => (
              <div key={item.id} className="space-y-1 rounded-lg border border-line p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">v{item.version}</span>
                  <span className="text-xs text-ink-500">
                    {new Date(item.published_at).toLocaleString()}
                  </span>
                </div>
                <p className="text-xs text-ink-500">
                  基于 revision {item.source_revision} · {item.published_by}
                </p>
                {item.note && <p className="text-xs text-ink-700">{item.note}</p>}
                {editable && (
                  <button
                    className="text-xs text-brand-700"
                    disabled={restore.isPending}
                    type="button"
                    onClick={() => restore.mutate(item.version)}
                  >
                    恢复到草稿
                  </button>
                )}
              </div>
            ))}
          </aside>
        </div>
      )}

      {showPublish && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">发布新版本</h2>
            <p className="text-xs text-ink-500">
              发布会先保存当前草稿，再由服务端做完整校验；校验失败会列出问题。
            </p>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>发布说明</span>
              <input
                className="input"
                maxLength={500}
                value={publishNote}
                onChange={(event) => setPublishNote(event.target.value)}
              />
            </label>
            {modalError && <p className="text-sm text-coral-600">{modalError}</p>}
            <div className="flex justify-end gap-2">
              <button className="btn-secondary" type="button" onClick={closePublish}>
                取消
              </button>
              <button
                className="btn-primary"
                disabled={publish.isPending || save.isPending}
                type="button"
                onClick={() => void confirmPublish()}
              >
                发布
              </button>
            </div>
          </div>
        </div>
      )}

      {showRun && latest && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">运行 v{latest.version}</h2>
            {runVersionQuery.isPending && (
              <p className="text-sm text-ink-500">加载中…</p>
            )}
            {runVersionQuery.isError && (
              <p className="text-sm text-coral-600">
                {errorText(runVersionQuery.error)}
              </p>
            )}
            {runVersionQuery.isSuccess && runFields.length === 0 && (
              <p className="text-sm text-ink-500">这个工作流没有声明入参。</p>
            )}
            {runVersionQuery.isSuccess &&
              runFields.map((field) => (
                <label key={field.key} className="block space-y-1 text-sm text-ink-500">
                  <span>
                    {field.label || field.key}
                    {field.required ? " *" : ""}
                  </span>
                  {field.type === "boolean" ? (
                    <input
                      type="checkbox"
                      checked={Boolean(runValues[field.key])}
                      onChange={(event) =>
                        setRunValues({ ...runValues, [field.key]: event.target.checked })
                      }
                    />
                  ) : (
                    <input
                      className="input"
                      type={field.type === "number" ? "number" : "text"}
                      value={String(runValues[field.key] ?? "")}
                      onChange={(event) =>
                        setRunValues({ ...runValues, [field.key]: event.target.value })
                      }
                    />
                  )}
                </label>
              ))}
            {modalError && <p className="text-sm text-coral-600">{modalError}</p>}
            <div className="flex justify-end gap-2">
              <button className="btn-secondary" type="button" onClick={closeRun}>
                取消
              </button>
              <button
                className="btn-primary"
                disabled={startRun.isPending || !runVersionQuery.isSuccess}
                type="button"
                onClick={submitRun}
              >
                开始运行
              </button>
            </div>
          </div>
        </div>
      )}

      {scheduleForm && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink-900/20 p-4">
          <div className="w-full max-w-md space-y-3 rounded-xl bg-surface p-6">
            <h2 className="text-lg font-semibold">
              {scheduleForm.id ? "编辑定时" : "新建定时"}
            </h2>
            <div className="flex gap-2 text-sm">
              {(["daily", "weekly"] as const).map((kind) => (
                <button
                  key={kind}
                  className={
                    scheduleForm.kind === kind ? "btn-primary" : "btn-secondary"
                  }
                  type="button"
                  onClick={() => setScheduleForm({ ...scheduleForm, kind })}
                >
                  {kind === "daily" ? "每天" : "每周"}
                </button>
              ))}
            </div>
            {scheduleForm.kind === "weekly" && (
              <div className="flex flex-wrap gap-2 text-sm">
                {WEEKDAYS.map((day) => {
                  const checked = scheduleForm.weekdays.includes(day.value);
                  return (
                    <label key={day.value} className="flex items-center gap-1 text-ink-700">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            weekdays: event.target.checked
                              ? [...scheduleForm.weekdays, day.value].sort()
                              : scheduleForm.weekdays.filter((value) => value !== day.value),
                          })
                        }
                      />
                      周{day.label}
                    </label>
                  );
                })}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <label className="block space-y-1 text-sm text-ink-500">
                <span>时间</span>
                <input
                  className="input"
                  type="time"
                  value={scheduleForm.time}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, time: event.target.value })
                  }
                />
              </label>
              <label className="block space-y-1 text-sm text-ink-500">
                <span>时区</span>
                <input
                  className="input"
                  value={scheduleForm.timezone}
                  onChange={(event) =>
                    setScheduleForm({ ...scheduleForm, timezone: event.target.value })
                  }
                />
              </label>
            </div>
            {scheduleVersionQuery.isPending && (
              <p className="text-sm text-ink-500">加载中…</p>
            )}
            {scheduleVersionQuery.isError && (
              <p className="text-sm text-coral-600">
                {errorText(scheduleVersionQuery.error)}
              </p>
            )}
            {scheduleVersionQuery.isSuccess && scheduleFields.length > 0 && (
              <div className="space-y-2">
                <p className="text-xs text-ink-500">运行入参</p>
                {scheduleFields.map((field) => (
                  <label key={field.key} className="block space-y-1 text-sm text-ink-500">
                    <span>{field.label || field.key}</span>
                    {field.type === "boolean" ? (
                      <input
                        type="checkbox"
                        checked={Boolean(scheduleForm.values[field.key])}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            values: {
                              ...scheduleForm.values,
                              [field.key]: event.target.checked,
                            },
                          })
                        }
                      />
                    ) : (
                      <input
                        className="input"
                        type={field.type === "number" ? "number" : "text"}
                        value={String(scheduleForm.values[field.key] ?? "")}
                        onChange={(event) =>
                          setScheduleForm({
                            ...scheduleForm,
                            values: {
                              ...scheduleForm.values,
                              [field.key]: event.target.value,
                            },
                          })
                        }
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
            {modalError && <p className="text-sm text-coral-600">{modalError}</p>}
            <div className="flex justify-end gap-2">
              <button
                className="btn-secondary"
                type="button"
                onClick={closeSchedule}
              >
                取消
              </button>
              <button
                className="btn-primary"
                disabled={saveSchedule.isPending || !scheduleVersionQuery.isSuccess}
                type="button"
                onClick={submitSchedule}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
