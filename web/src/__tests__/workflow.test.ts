import { describe, expect, it } from "vitest";
import {
  addNode,
  approvalSourceLabel,
  canCancelWorkflowRun,
  canDecideWorkflowApproval,
  canEditWorkflow,
  canResumeWorkflowRun,
  canRunWorkflow,
  conditionTargets,
  connectNodes,
  emptyGraph,
  executedEdgeIds,
  graphScale,
  inputFields,
  inputPayload,
  isRunTerminal,
  latestNodeStatuses,
  moveNode,
  nodeKindLabel,
  nodeLabel,
  nodeStatusBadge,
  nodeStatusLabel,
  reachableNodes,
  removeNode,
  runStatusBadge,
  runStatusLabel,
  scheduleText,
  topoOrder,
  triggerLabel,
  updateNodeData,
  upstreamNodeIds,
  variableCandidates,
} from "../workflows";
import { visibleNavItems } from "../auth";
import type {
  Graph,
  WorkflowApproval,
  WorkflowRun,
  WorkflowSchedule,
} from "../types/workflow";

const graph: Graph = {
  nodes: [
    { id: "in", type: "input", data: { label: "输入", fields: [{ key: "topic", label: "主题", type: "string", required: true }] } },
    { id: "a", type: "tool", data: { label: "读画像", tool: "profile.list", args: {}, retry: 1 } },
    { id: "b", type: "agent", data: { label: "写笔记", goal: "写一版", tools: [], budget: {}, retry: 0 } },
    { id: "cond", type: "condition", data: { label: "是否通过", left: "{{nodes.b.output.result_text}}", operator: "contains", value: "通过" } },
    { id: "yes", type: "tool", data: { label: "建草稿", tool: "publish.create_draft_job", args: {}, retry: 0 } },
    { id: "no", type: "tool", data: { label: "记录失败", tool: "profile.list", args: {}, retry: 0 } },
    { id: "out", type: "output", data: { label: "输出" } },
  ],
  edges: [
    { id: "e1", source: "in", target: "a" },
    { id: "e2", source: "a", target: "b" },
    { id: "e3", source: "b", target: "cond" },
    { id: "e4", source: "cond", sourceHandle: "true", target: "yes" },
    { id: "e5", source: "cond", sourceHandle: "false", target: "no" },
    { id: "e6", source: "yes", target: "out" },
    { id: "e7", source: "no", target: "out" },
  ],
};

const viewer = { id: "u1", role: "viewer" as const };
const editor = { id: "u2", role: "editor" as const };
const admin = { id: "u3", role: "admin" as const };

function run(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    id: "r1",
    workflow_id: "w1",
    workflow_version_id: "v1",
    version: 1,
    trigger: "manual",
    triggered_by: "u2",
    schedule_id: null,
    scheduled_for: null,
    status: "queued",
    inputs: {},
    result: {},
    error_code: "",
    error_message: "",
    usage: {},
    attempts: 0,
    resume_count: 0,
    created_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function approval(overrides: Partial<WorkflowApproval> = {}): WorkflowApproval {
  return {
    id: "ap1",
    run_id: "r1",
    workflow_id: "w1",
    source: "workflow",
    kind: "node",
    node_id: "rev",
    tool_name: "",
    risk: "low",
    title: "确认草稿",
    payload: {},
    status: "pending",
    note: "",
    expires_at: null,
    created_at: null,
    ...overrides,
  };
}

describe("图结构纯函数", () => {
  it("收集上游节点集合（不含自身）", () => {
    expect([...upstreamNodeIds(graph, "b")].sort()).toEqual(["a", "in"]);
    expect(upstreamNodeIds(graph, "in").size).toBe(0);

    const diamond: Graph = {
      nodes: [
        { id: "s", type: "input", data: {} },
        { id: "p1", type: "tool", data: {} },
        { id: "p2", type: "tool", data: {} },
        { id: "t", type: "output", data: {} },
      ],
      edges: [
        { id: "d1", source: "s", target: "p1" },
        { id: "d2", source: "s", target: "p2" },
        { id: "d3", source: "p1", target: "t" },
        { id: "d4", source: "p2", target: "t" },
      ],
    };
    const parents = upstreamNodeIds(diamond, "t");
    expect([...parents].sort()).toEqual(["p1", "p2", "s"]);
    expect(parents.size).toBe(3);
    expect(parents.has("t")).toBe(false);
  });

  it("拓扑序满足依赖，有环时返回 null", () => {
    const order = topoOrder(graph);
    expect(order).not.toBeNull();
    expect(order!.indexOf("in")).toBeLessThan(order!.indexOf("a"));
    expect(order!.indexOf("b")).toBeLessThan(order!.indexOf("cond"));

    const withGhost: Graph = {
      ...graph,
      edges: [
        ...graph.edges,
        { id: "g1", source: "ghost", target: "a" },
        { id: "g2", source: "out", target: "ghost" },
      ],
    };
    const ghostOrder = topoOrder(withGhost);
    expect(ghostOrder).not.toBeNull();
    expect([...ghostOrder!].sort()).toEqual(
      graph.nodes.map((node) => node.id).sort(),
    );
    expect(ghostOrder!.indexOf("b")).toBeLessThan(ghostOrder!.indexOf("cond"));

    const cyclic: Graph = {
      ...graph,
      edges: [...graph.edges, { id: "e8", source: "out", target: "a" }],
    };
    expect(topoOrder(cyclic)).toBeNull();
  });

  it("条件节点的两个出边句柄", () => {
    expect(conditionTargets(graph, "cond")).toEqual({ yes: "yes", no: "no" });
    expect(conditionTargets(graph, "a")).toEqual({ yes: null, no: null });
  });

  it("按条件决策计算可达节点", () => {
    const pass = reachableNodes(graph, { cond: true });
    expect(pass.has("yes")).toBe(true);
    expect(pass.has("no")).toBe(false);
    expect(pass.has("out")).toBe(true);

    const fail = reachableNodes(graph, { cond: false });
    expect(fail.has("no")).toBe(true);
    expect(fail.has("yes")).toBe(false);
  });

  it("变量候选只包含输入字段与上游节点", () => {
    const options = variableCandidates(graph, "b", inputFields(graph));
    const values = options.map((item) => item.value);
    expect(values).toContain("{{inputs.topic}}");
    expect(values).toContain("{{nodes.a.output}}");
    expect(values).toContain("{{nodes.a.output.result_text}}");
    expect(values.some((value) => value.startsWith("{{nodes.b."))).toBe(false);
    expect(values.some((value) => value.startsWith("{{nodes.cond."))).toBe(false);
    expect(options.find((item) => item.value === "{{inputs.topic}}")?.label).toBe(
      "输入 · 主题",
    );
  });

  it("空图保留输入与输出节点且没有边", () => {
    const blank = emptyGraph();
    expect(blank.nodes.map((node) => node.type)).toEqual(["input", "output"]);
    expect(blank.edges).toEqual([]);
    expect(inputFields(blank)).toEqual([]);
  });

  it("规模提示在 90% 与超限时升级", () => {
    const make = (nodes: number, edges: number): Graph => ({
      nodes: Array.from({ length: nodes }, (_, index) => ({
        id: `n${index}`,
        type: "tool",
        data: {},
      })),
      edges: Array.from({ length: edges }, (_, index) => ({
        id: `e${index}`,
        source: "n0",
        target: "n1",
      })),
    });
    expect(graphScale(make(5, 5)).level).toBe("ok");
    expect(graphScale(make(45, 10)).level).toBe("warn");
    expect(graphScale(make(51, 10)).level).toBe("over");
    expect(graphScale(make(10, 90)).level).toBe("warn");
    expect(graphScale(make(10, 100)).level).toBe("warn");
    expect(graphScale(make(10, 101)).level).toBe("over");
    expect(graphScale(make(5, 5)).text).toBe("5/50 节点 · 5/100 边");
  });
});

describe("标签与状态", () => {
  it("节点类型与显示名", () => {
    expect(nodeKindLabel("agent")).toBe("Agent");
    expect(nodeKindLabel("unknown")).toBe("unknown");
    expect(nodeLabel({ id: "n1", type: "tool", data: {} })).toBe("n1");
    expect(nodeLabel({ id: "n1", type: "tool", data: { label: "读画像" } })).toBe(
      "读画像",
    );
  });

  it("节点状态标签与徽标回退", () => {
    expect(nodeStatusLabel("ok")).toBe("成功");
    expect(nodeStatusLabel("skipped")).toBe("已跳过");
    expect(nodeStatusLabel("mystery")).toBe("mystery");
    expect(nodeStatusBadge("error")).toContain("wine");
    expect(nodeStatusBadge("running")).toContain("sage");
  });

  it("运行状态、触发方式与审批来源", () => {
    expect(runStatusLabel("waiting_approval")).toBe("待审核");
    expect(runStatusLabel("succeeded")).toBe("已完成");
    expect(triggerLabel("schedule")).toBe("定时");
    expect(triggerLabel("manual")).toBe("手动");
    expect(approvalSourceLabel("workflow")).toBe("工作流审核");
    expect(approvalSourceLabel("agent")).toBe("Agent 工具确认");
  });

  it("终态判定", () => {
    expect(isRunTerminal("succeeded")).toBe(true);
    expect(isRunTerminal("failed")).toBe(true);
    expect(isRunTerminal("cancelled")).toBe(true);
    expect(isRunTerminal("running")).toBe(false);
    expect(isRunTerminal("waiting_approval")).toBe(false);
  });
});

describe("权限助手", () => {
  it("只有 admin/owner 能编辑工作流", () => {
    expect(canEditWorkflow("editor")).toBe(false);
    expect(canEditWorkflow("admin")).toBe(true);
    expect(canEditWorkflow("owner")).toBe(true);
  });

  it("运行需要写权限且已有发布版本", () => {
    expect(canRunWorkflow("editor", true)).toBe(true);
    expect(canRunWorkflow("editor", false)).toBe(false);
    expect(canRunWorkflow("viewer", true)).toBe(false);
  });

  it("手动运行排除发起人，定时运行管理员可见", () => {
    expect(canDecideWorkflowApproval(approval(), run(), admin)).toBe(true);
    expect(canDecideWorkflowApproval(approval(), run(), editor)).toBe(false);
    expect(
      canDecideWorkflowApproval(approval(), run({ triggered_by: "u3" }), admin),
    ).toBe(false);
    expect(
      canDecideWorkflowApproval(
        approval(),
        run({ trigger: "schedule", triggered_by: null }),
        admin,
      ),
    ).toBe(true);
    expect(
      canDecideWorkflowApproval(approval({ status: "approved" }), run(), admin),
    ).toBe(false);
  });

  it("取消与续跑的归属规则", () => {
    expect(canCancelWorkflowRun(run(), editor)).toBe(true);
    expect(canCancelWorkflowRun(run(), admin)).toBe(false);
    expect(
      canCancelWorkflowRun(run({ trigger: "schedule", triggered_by: null }), admin),
    ).toBe(true);
    expect(canCancelWorkflowRun(run({ status: "succeeded" }), editor)).toBe(false);

    expect(canResumeWorkflowRun(run({ status: "failed" }), editor)).toBe(true);
    expect(canResumeWorkflowRun(run({ status: "failed", resume_count: 10 }), editor)).toBe(
      false,
    );
    expect(canResumeWorkflowRun(run({ status: "running" }), editor)).toBe(false);
    expect(
      canResumeWorkflowRun(
        run({ status: "failed", trigger: "schedule", triggered_by: null }),
        editor,
      ),
    ).toBe(false);
  });
});

describe("定时文案", () => {
  const base = {
    id: "s1",
    workflow_version_id: "v1",
    inputs: {},
    enabled: true,
    next_run_at: null,
    last_run_at: null,
  };

  it("每日与每周", () => {
    const daily: WorkflowSchedule = {
      ...base,
      cadence: { kind: "daily", weekdays: [], time: "09:00", timezone: "Asia/Shanghai" },
    };
    const weekly: WorkflowSchedule = {
      ...base,
      cadence: {
        kind: "weekly",
        weekdays: [4, 1],
        time: "08:30",
        timezone: "Asia/Shanghai",
      },
    };
    expect(scheduleText(daily)).toBe("每天 09:00（Asia/Shanghai）");
    expect(scheduleText(weekly)).toBe("每周一、四 08:30（Asia/Shanghai）");
  });

  it("缺少时区时不显示括号", () => {
    const schedule: WorkflowSchedule = {
      ...base,
      cadence: { kind: "daily", weekdays: [], time: "07:05", timezone: "" },
    };
    expect(scheduleText(schedule)).toBe("每天 07:05");
  });
});

describe("工作流导航", () => {
  it("editor 及以上可见，viewer 不可见", () => {
    const editorItems = visibleNavItems({ role: "editor", is_platform_admin: false });
    expect(editorItems.some((item) => item.to === "/workflows" && item.label === "工作流")).toBe(
      true,
    );
    const viewerItems = visibleNavItems({ role: "viewer", is_platform_admin: false });
    expect(viewerItems.some((item) => item.to === "/workflows")).toBe(false);
  });
});

describe("图编辑帮助函数", () => {
  it("按类型生成默认数据与唯一 id", () => {
    const one = addNode(emptyGraph(), "tool", { x: 300, y: 120 });
    const added = one.nodes.find((node) => node.id === "t1");
    expect(added).toBeDefined();
    expect(added!.type).toBe("tool");
    expect(added!.data).toMatchObject({ label: "工具", tool: "", args: {}, retry: 0 });
    expect(added!.position).toEqual({ x: 300, y: 120 });

    const two = addNode(one, "tool", { x: 300, y: 240 });
    expect(two.nodes.map((node) => node.id)).toContain("t2");
  });

  it("条件节点必须带句柄，非条件节点不得带句柄", () => {
    const withCondition = addNode(graph, "condition", { x: 0, y: 0 });
    const plain = connectNodes(withCondition, "a", "out", null);
    expect(plain.error).toBeNull();
    expect(plain.graph.edges.at(-1)).toMatchObject({
      source: "a",
      target: "out",
    });

    const handled = connectNodes(withCondition, "a", "out", "true");
    expect(handled.error).toBe("只有条件节点可以指定出边句柄");

    const missing = connectNodes(withCondition, "cond", "out", null);
    expect(missing.error).toBe("条件节点必须从「是」或「否」出边连接");

    const taken = connectNodes(withCondition, "cond", "out", "true");
    expect(taken.error).toBe("该分支已有连线");
  });

  it("拒绝自连、输入入边与重复连线", () => {
    expect(connectNodes(graph, "a", "a").error).toBe("不能连接到自己");
    expect(connectNodes(graph, "a", "in").error).toBe("输入节点不能有入边");
    expect(connectNodes(graph, "a", "b").error).toBe("连线已存在");
  });

  it("删除节点同时删除关联边，更新位置与数据保持不变式", () => {
    const removed = removeNode(graph, "a");
    expect(removed.nodes.some((node) => node.id === "a")).toBe(false);
    expect(
      removed.edges.some((edge) => edge.source === "a" || edge.target === "a"),
    ).toBe(false);

    const moved = moveNode(graph, "a", { x: 12, y: 34 });
    expect(moved.nodes.find((node) => node.id === "a")!.position).toEqual({
      x: 12,
      y: 34,
    });
    expect(moved.nodes.length).toBe(graph.nodes.length);

    const patched = updateNodeData(graph, "a", { label: "读画像（改）" });
    expect(patched.nodes.find((node) => node.id === "a")!.data.label).toBe(
      "读画像（改）",
    );
    expect(patched.nodes.find((node) => node.id === "b")!.data).toEqual(
      graph.nodes.find((node) => node.id === "b")!.data,
    );
  });

  it("已执行边只包含两端都成功的边", () => {
    const runs = [
      { node_id: "in", status: "ok" },
      { node_id: "a", status: "ok" },
      { node_id: "b", status: "ok" },
      { node_id: "cond", status: "ok" },
      { node_id: "yes", status: "skipped" },
      { node_id: "no", status: "ok" },
      { node_id: "out", status: "ok" },
    ] as never;
    const ids = executedEdgeIds(graph, runs);
    expect(ids.has("e2")).toBe(true);
    expect(ids.has("e5")).toBe(true);
    expect(ids.has("e4")).toBe(false);
    expect(ids.has("e6")).toBe(false);
  });
});

describe("运行入参转换", () => {
  it("按字段类型转换，跳过空的可选值", () => {
    const fields = [
      { key: "topic", type: "string" as const, required: true },
      { key: "limit", type: "number" as const },
      { key: "dry_run", type: "boolean" as const },
    ];
    expect(
      inputPayload(fields, { topic: "秋季护肤", limit: "5", dry_run: false }),
    ).toEqual({ topic: "秋季护肤", limit: 5, dry_run: false });
    expect(inputPayload(fields, { topic: "只有主题" })).toEqual({
      topic: "只有主题",
      dry_run: false,
    });
    expect(inputPayload(fields, { topic: "", limit: "" })).toEqual({
      dry_run: false,
    });
  });
});

describe("运行状态汇总", () => {
  it("同一节点取最大 attempt 的状态，并给运行状态徽标", () => {
    const runs = [
      { node_id: "a", attempt: 1, status: "error" },
      { node_id: "a", attempt: 2, status: "ok" },
      { node_id: "b", attempt: 1, status: "skipped" },
    ] as never;
    expect(latestNodeStatuses(runs)).toEqual({ a: "ok", b: "skipped" });
    expect(runStatusBadge("succeeded")).toContain("sage");
    expect(runStatusBadge("failed")).toContain("wine");
    expect(runStatusBadge("waiting_approval")).toContain("sage-100");
    expect(nodeStatusBadge("waiting")).toContain("warm");
  });
});
