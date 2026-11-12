import { describe, expect, it } from "vitest";
import { validatePasswordChange } from "../changePassword";

describe("validatePasswordChange", () => {
  it("requires all fields", () => {
    expect(validatePasswordChange("", "new-password", "new-password")).toBe(
      "请输入当前密码",
    );
    expect(validatePasswordChange("old-password", "", "")).toBe(
      "新密码至少 8 位",
    );
    expect(validatePasswordChange("old-password", "new-password", "")).toBe(
      "两次输入的新密码不一致",
    );
  });

  it("enforces length and rejects reuse", () => {
    expect(validatePasswordChange("old-password", "short", "short")).toBe(
      "新密码至少 8 位",
    );
    expect(
      validatePasswordChange("old-password", "x".repeat(129), "x".repeat(129)),
    ).toBe("新密码不能超过 128 位");
    expect(
      validatePasswordChange("same-password", "same-password", "same-password"),
    ).toBe("新密码不能与当前密码相同");
  });

  it("accepts a valid change", () => {
    expect(
      validatePasswordChange("old-password", "new-password", "new-password"),
    ).toBeNull();
  });
});
