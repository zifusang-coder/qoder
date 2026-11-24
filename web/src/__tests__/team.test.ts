import { describe, expect, it } from "vitest";
import {
  canEditMember,
  canManageMembers,
  memberStatusLabel,
  roleLabel,
} from "../team";

describe("team helpers", () => {
  it("labels the four roles with a fallback", () => {
    expect(roleLabel("viewer")).toBe("只读");
    expect(roleLabel("editor")).toBe("编辑");
    expect(roleLabel("admin")).toBe("管理员");
    expect(roleLabel("owner")).toBe("所有者");
    expect(roleLabel("unknown")).toBe("unknown");
  });

  it("allows only owners to manage members and never themselves", () => {
    expect(canManageMembers("admin")).toBe(false);
    expect(canManageMembers("owner")).toBe(true);
    expect(canEditMember({ id: "u1", role: "owner" }, { id: "u1" })).toBe(false);
    expect(canEditMember({ id: "u1", role: "owner" }, { id: "u2" })).toBe(true);
    expect(canEditMember({ id: "u1", role: "admin" }, { id: "u2" })).toBe(false);
  });

  it("labels active state", () => {
    expect(memberStatusLabel(true)).toBe("启用");
    expect(memberStatusLabel(false)).toBe("已停用");
  });
});
