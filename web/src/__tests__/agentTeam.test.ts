import { describe, expect, it } from "vitest";
import {
  canEditTeamRoles, canStartTeamRun, defaultTeamBudget, entryKindLabel,
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