import { describe, expect, it } from "vitest";
import {
  API_KEY_SCOPES,
  describeScopes,
  formatKeyTime,
  isKeyActive,
  scopeLabel,
} from "../apiKeys";

describe("scopeLabel", () => {
  it("maps every supported scope to a readable label", () => {
    for (const item of API_KEY_SCOPES) {
      expect(scopeLabel(item.value)).toBe(item.label);
    }
  });

  it("keeps unknown scopes visible instead of hiding them", () => {
    expect(scopeLabel("admin:everything")).toBe("admin:everything");
  });
});

describe("describeScopes", () => {
  it("joins the labels in the stored order", () => {
    expect(describeScopes(["workflow:read", "agent:run"])).toBe(
      "读取工作流运行、创建 Agent 运行",
    );
  });

  it("reports empty scope lists explicitly", () => {
    expect(describeScopes([])).toBe("无范围");
  });
});

describe("isKeyActive", () => {
  it("treats a key without revoked_at as active", () => {
    expect(isKeyActive({ revoked_at: null })).toBe(true);
  });

  it("treats a revoked key as inactive", () => {
    expect(isKeyActive({ revoked_at: "2026-11-23T00:00:00Z" })).toBe(false);
  });
});

describe("formatKeyTime", () => {
  it("renders an empty marker when the key was never used", () => {
    expect(formatKeyTime(null)).toBe("从未使用");
  });

  it("renders unparseable values as unknown", () => {
    expect(formatKeyTime("not-a-date")).toBe("未知");
  });
});
