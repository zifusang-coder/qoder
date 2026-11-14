import { describe, expect, it } from "vitest";
import {
  agentStatusLabel,
  artifactLabel,
  artifactLink,
  budgetPercent,
  canCancelRun,
  canCreateRun,
  canDecideApproval,
  canRetryRun,
  firstLine,
  formatElapsed,
  groupSteps,
  isRunActive,
  isRunTerminal,
  stepKindLabel,
  stepStatusLabel,
  usageTokens,
  type AgentRun,
  type AgentRunDetail,
  type AgentStatus,
  type AgentStep,
  type Artifact,
  type StepGroup,
} from "../agent";

const step = (
  step_index: number,
  kind: string,
  tool_name = "",
  status = "ok",
): AgentStep => ({
  step_index,
  kind,
  tool_name,
  status,
  input: {},
  output: {},
  error: "",
  duration_ms: 0,
  started_at: null,
  finished_at: null,
});

// The exported types must stay importable by the assistant page (Task 13);
// these annotations fail the typecheck if a field disappears.
const runFixture: AgentRun = {
  id: "r1",
  user_id: "u1",
  goal: "列出我的项目",
  mode: "single",
  status: "running",
  plan: {},
  result_text: "",
  result: { artifacts: [{ type: "project", id: "p1", title: "秋季护肤" }] },
  error_code: "",
  error_message: "",
  budget: { max_steps: 10 },
  usage: { prompt_tokens: 1, completion_tokens: 2, steps: 1 },
  step_count: 1,
  created_at: "2026-11-13T10:00:00Z",
  started_at: "2026-11-13T10:00:01Z",
  finished_at: null,
  pending_approval: null,
};

const detailFixture: AgentRunDetail = { ...runFixture, steps: [step(0, "llm")] };
const statusFixture: AgentStatus = "waiting_approval";
const artifactFixture: Artifact = { type: "content", id: "c1" };
const groupFixture: StepGroup = { llm: null, actions: [] };

describe("exported types", () => {
  it("keep the shapes the assistant page builds on", () => {
    expect(runFixture.step_count).toBe(1);
    expect(runFixture.budget.max_steps).toBe(10);
    expect(runFixture.pending_approval).toBeNull();
    expect(detailFixture.steps).toHaveLength(1);
    expect(groupSteps(detailFixture.steps)).toHaveLength(1);
    expect(agentStatusLabel(statusFixture)).toBe("待确认");
    expect(artifactLabel(artifactFixture.type ?? "")).toBe("内容");
    expect(groupFixture).toEqual({ llm: null, actions: [] });
  });
});

describe("agentStatusLabel", () => {
  it("labels every known run status in Chinese", () => {
    expect(agentStatusLabel("queued")).toBe("排队中");
    expect(agentStatusLabel("running")).toBe("执行中");
    expect(agentStatusLabel("waiting_approval")).toBe("待确认");
    expect(agentStatusLabel("succeeded")).toBe("已完成");
    expect(agentStatusLabel("failed")).toBe("失败");
    expect(agentStatusLabel("cancelled")).toBe("已取消");
  });

  it("falls back to the raw value for an unknown status", () => {
    expect(agentStatusLabel("paused")).toBe("paused");
  });
});

describe("stepStatusLabel", () => {
  it("labels step statuses and falls back to the raw value", () => {
    expect(stepStatusLabel("ok")).toBe("成功");
    expect(stepStatusLabel("interrupted")).toBe("已中断");
    expect(stepStatusLabel("mystery")).toBe("mystery");
  });
});

describe("stepKindLabel", () => {
  it("labels step kinds and falls back to the raw value", () => {
    expect(stepKindLabel("tool")).toBe("工具");
    expect(stepKindLabel("approval")).toBe("审批");
    expect(stepKindLabel("mystery")).toBe("mystery");
  });
});

describe("isRunActive / isRunTerminal", () => {
  it("treats queued and running as active", () => {
    expect(isRunActive("queued")).toBe(true);
    expect(isRunActive("running")).toBe(true);
    expect(isRunActive("waiting_approval")).toBe(false);
  });

  it("treats finished states as terminal only", () => {
    expect(isRunTerminal("succeeded")).toBe(true);
    expect(isRunTerminal("failed")).toBe(true);
    expect(isRunTerminal("cancelled")).toBe(true);
    expect(isRunTerminal("waiting_approval")).toBe(false);
    expect(isRunTerminal("running")).toBe(false);
  });
});

describe("groupSteps", () => {
  it("attaches tool and approval steps to the preceding llm step", () => {
    const groups = groupSteps([
      step(0, "llm"),
      step(1, "tool", "project.list"),
      step(2, "approval"),
      step(3, "llm"),
      step(4, "tool", "content.create_version"),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].llm?.step_index).toBe(0);
    expect(groups[0].actions.map((item) => item.step_index)).toEqual([1, 2]);
    expect(groups[1].llm?.step_index).toBe(3);
    expect(groups[1].actions.map((item) => item.step_index)).toEqual([4]);
  });

  it("keeps a leading action group when the first step is not llm", () => {
    const groups = groupSteps([step(0, "tool", "project.list"), step(1, "llm")]);
    expect(groups).toHaveLength(2);
    expect(groups[0].llm).toBeNull();
    expect(groups[0].actions.map((item) => item.step_index)).toEqual([0]);
    expect(groups[1].llm?.step_index).toBe(1);
  });

  it("returns an empty list for no steps", () => {
    expect(groupSteps([])).toEqual([]);
  });
});

describe("permissions", () => {
  it("lets editors and above create runs", () => {
    expect(canCreateRun("viewer")).toBe(false);
    expect(canCreateRun("editor")).toBe(true);
    expect(canCreateRun("admin")).toBe(true);
    expect(canCreateRun("owner")).toBe(true);
  });

  it("only lets a non-creator admin or owner decide an approval", () => {
    const run = { status: "waiting_approval", user_id: "u1" };
    expect(canDecideApproval(run, { id: "u2", role: "owner" })).toBe(true);
    expect(canDecideApproval(run, { id: "u2", role: "admin" })).toBe(true);
    expect(canDecideApproval(run, { id: "u1", role: "owner" })).toBe(false);
    expect(canDecideApproval(run, { id: "u2", role: "editor" })).toBe(false);
    expect(
      canDecideApproval(
        { status: "running", user_id: "u1" },
        { id: "u2", role: "owner" },
      ),
    ).toBe(false);
  });

  // The backend returns 403 when an admin cancels somebody else's run, so
  // only the creator (with write access) may cancel, never an admin.
  it("only lets the creator cancel a live run", () => {
    const run = { status: "running", user_id: "u1" };
    expect(canCancelRun(run, { id: "u1", role: "editor" })).toBe(true);
    expect(
      canCancelRun({ status: "queued", user_id: "u1" }, { id: "u1", role: "owner" }),
    ).toBe(true);
    expect(
      canCancelRun(
        { status: "waiting_approval", user_id: "u1" },
        { id: "u1", role: "admin" },
      ),
    ).toBe(true);
    expect(canCancelRun(run, { id: "u2", role: "admin" })).toBe(false);
    expect(canCancelRun(run, { id: "u2", role: "owner" })).toBe(false);
    expect(canCancelRun(run, { id: "u2", role: "editor" })).toBe(false);
    expect(
      canCancelRun(run, { id: "u1", role: "viewer" }),
    ).toBe(false);
    expect(
      canCancelRun({ status: "succeeded", user_id: "u1" }, { id: "u1", role: "editor" }),
    ).toBe(false);
    expect(
      canCancelRun({ status: "failed", user_id: "u1" }, { id: "u1", role: "editor" }),
    ).toBe(false);
  });

  it("only lets editors retry a failed or cancelled run", () => {
    expect(canRetryRun("failed", "editor")).toBe(true);
    expect(canRetryRun("cancelled", "editor")).toBe(true);
    expect(canRetryRun("failed", "viewer")).toBe(false);
    expect(canRetryRun("running", "editor")).toBe(false);
    expect(canRetryRun("succeeded", "owner")).toBe(false);
  });
});

describe("usageTokens / budgetPercent", () => {
  it("sums prompt and completion tokens", () => {
    expect(usageTokens({ prompt_tokens: 120, completion_tokens: 30 })).toBe(150);
    expect(usageTokens({})).toBe(0);
  });

  it("clamps the percentage into 0..100 and guards a zero cap", () => {
    expect(budgetPercent(3, 10)).toBe(30);
    expect(budgetPercent(15, 10)).toBe(100);
    expect(budgetPercent(-1, 10)).toBe(0);
    expect(budgetPercent(5, 0)).toBe(0);
    expect(budgetPercent(5, Number.NaN)).toBe(0);
  });
});

describe("formatElapsed", () => {
  const base = "2026-11-13T10:00:00Z";
  const at = (seconds: number) =>
    new Date(Date.parse(base) + seconds * 1000).toISOString();

  it("returns an empty string without a start time", () => {
    expect(formatElapsed(null, null)).toBe("");
  });

  it("formats seconds, minutes and hours", () => {
    expect(formatElapsed(base, at(15))).toBe("15s");
    expect(formatElapsed(base, at(95))).toBe("1m35s");
    expect(formatElapsed(base, at(3720))).toBe("1h2m");
  });

  it("uses the supplied now while a run is live", () => {
    expect(formatElapsed(base, null, Date.parse(base) + 42_000)).toBe("42s");
  });
});

describe("artifact helpers", () => {
  it("labels artifact types with a fallback", () => {
    expect(artifactLabel("project")).toBe("项目");
    expect(artifactLabel("content")).toBe("内容");
    expect(artifactLabel("media_job")).toBe("媒体任务");
    expect(artifactLabel("publish_job")).toBe("发布任务");
    expect(artifactLabel("profile")).toBe("画像");
    expect(artifactLabel("skill")).toBe("技能");
    expect(artifactLabel("mystery")).toBe("mystery");
  });

  it("links projects and profiles to their own pages", () => {
    expect(artifactLink({ type: "project", id: "p1" })).toBe("/projects/p1");
    expect(artifactLink({ type: "content", id: "p1" })).toBe("/projects/p1");
    expect(artifactLink({ type: "profile", id: "u1" })).toBe("/profiles");
    expect(artifactLink({ type: "publish_job", id: "j1" })).toBe("/publish");
    expect(artifactLink({ type: "media_job", id: "m1" })).toBe("/projects");
    expect(artifactLink({})).toBe("/projects");
  });
});

describe("firstLine", () => {
  it("returns the first non-empty line, truncated", () => {
    expect(firstLine("\n  第一行目标  \n第二行")).toBe("第一行目标");
    expect(firstLine("a".repeat(80))).toBe(`${"a".repeat(60)}…`);
    expect(firstLine("   ")).toBe("");
  });
});
