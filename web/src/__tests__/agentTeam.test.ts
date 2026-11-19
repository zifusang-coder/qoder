import { describe, expect, it } from "vitest";
import {
  canCancelTeamRun, canDecideTeamChildApproval, canEditTeamRoles, canStartTeamRun,
  defaultTeamBudget, entryKindLabel,
  roleLabel, teamTotals, validateParticipants, validateTeamBudget,
  type TeamRoleConfig,
} from "../agentTeam";

function config(role: string, enabled = true): TeamRoleConfig {
  return {
    role: role as TeamRoleConfig["role"], label: role, enabled,
    tools: [], default_tools: [], budget: {}, limits: {},
  };
}

describe("team pure functions", () => {
  it("default budget matches backend", () => {
    expect(defaultTeamBudget()).toEqual({
      max_tokens: 120000, max_tool_calls: 40, max_turns: 12, max_seconds: 900,
    });
  });

  it("participants require creator and reject disabled/unknown", () => {
    const configs = [config("scout"), config("creator"), config("critic", false)];
    expect(validateParticipants(["scout", "creator"], configs)).toBeNull();
    expect(validateParticipants(["scout"], configs)).toContain("creator");
    expect(validateParticipants(["critic", "creator"], configs)).toContain("关闭");
    expect(validateParticipants(["ghost", "creator"], configs)).toContain("未知");
    expect(validateParticipants([], configs)).not.toBeNull();
  });

  it("budget validation", () => {
    expect(validateTeamBudget({})).toBeNull();
    expect(validateTeamBudget({ max_tokens: 200000 })).toBeNull();
    expect(validateTeamBudget({ max_tokens: 200001 })).not.toBeNull();
    expect(validateTeamBudget({ max_tokens: 0 })).not.toBeNull();
    expect(validateTeamBudget({ max_steps: 5 })).not.toBeNull();
  });

  it("labels and permissions", () => {
    expect(roleLabel("creator")).toBe("创作");
    expect(roleLabel("ghost")).toBe("ghost");
    expect(entryKindLabel("delegation")).toBe("委派");
    expect(canEditTeamRoles("admin")).toBe(true);
    expect(canEditTeamRoles("editor")).toBe(false);
    expect(canStartTeamRun("editor")).toBe(true);
    expect(canStartTeamRun("viewer")).toBe(false);
  });

  it("teamTotals sums children usage", () => {
    const totals = teamTotals([
      { usage: { prompt_tokens: 10, completion_tokens: 5, tool_calls: 2 } },
      { usage: { prompt_tokens: 20, completion_tokens: 10, tool_calls: 3 } },
    ] as never);
    expect(totals.tokens).toBe(45);
    expect(totals.tool_calls).toBe(5);
  });
});

describe("canCancelTeamRun", () => {
  const run = { status: "running", user_id: "u1" };
  it("admin/owner may cancel others' live team runs", () => {
    expect(canCancelTeamRun(run, { id: "u2", role: "admin" })).toBe(true);
    expect(canCancelTeamRun(run, { id: "u2", role: "owner" })).toBe(true);
  });
  it("editor may cancel only own live team runs", () => {
    expect(canCancelTeamRun(run, { id: "u1", role: "editor" })).toBe(true);
    expect(canCancelTeamRun(run, { id: "u2", role: "editor" })).toBe(false);
  });
  it("viewer may never cancel", () => {
    expect(canCancelTeamRun(run, { id: "u1", role: "viewer" })).toBe(false);
  });
  it("live statuses include queued and waiting_approval", () => {
    expect(canCancelTeamRun({ status: "queued", user_id: "u1" }, { id: "u1", role: "editor" })).toBe(true);
    expect(canCancelTeamRun({ status: "waiting_approval", user_id: "u1" }, { id: "u1", role: "editor" })).toBe(true);
  });
  it("terminal statuses hide cancel for everyone", () => {
    for (const status of ["succeeded", "failed", "cancelled"]) {
      expect(canCancelTeamRun({ status, user_id: "u1" }, { id: "u1", role: "editor" })).toBe(false);
      expect(canCancelTeamRun({ status, user_id: "u1" }, { id: "u2", role: "admin" })).toBe(false);
    }
  });
});

describe("canDecideTeamChildApproval", () => {
  const child = { status: "waiting_approval" };
  const admin = { id: "a1", role: "admin" as const };
  it("非发起人的 admin/owner 可审批等待中的子运行", () => {
    expect(canDecideTeamChildApproval(child, "u1", admin)).toBe(true);
    expect(canDecideTeamChildApproval(child, "u1", { id: "o1", role: "owner" })).toBe(true);
  });
  it("发起人本人（即使是 owner）不能审批", () => {
    expect(canDecideTeamChildApproval(child, "a1", admin)).toBe(false);
  });
  it("editor/viewer 不能审批", () => {
    expect(canDecideTeamChildApproval(child, "u1", { id: "e1", role: "editor" })).toBe(false);
    expect(canDecideTeamChildApproval(child, "u1", { id: "v1", role: "viewer" })).toBe(false);
  });
  it("子运行非 waiting_approval 时不可审批", () => {
    for (const status of ["queued", "running", "succeeded", "failed", "cancelled"]) {
      expect(canDecideTeamChildApproval({ status }, "u1", admin)).toBe(false);
    }
  });
});
