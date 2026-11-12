import { describe, expect, it } from "vitest";
import {
  atLeast,
  canWrite,
  defaultPath,
  isAdmin,
  isOwner,
  mustChangePath,
  visibleNavItems,
} from "../auth";

describe("role capabilities", () => {
  it("ranks the four fixed roles", () => {
    expect(atLeast("owner", "admin")).toBe(true);
    expect(atLeast("admin", "admin")).toBe(true);
    expect(atLeast("editor", "admin")).toBe(false);
    expect(atLeast("viewer", "editor")).toBe(false);
  });

  it("derives write and admin capabilities", () => {
    expect(canWrite("viewer")).toBe(false);
    expect(canWrite("editor")).toBe(true);
    expect(isAdmin("editor")).toBe(false);
    expect(isAdmin("admin")).toBe(true);
    expect(isOwner("admin")).toBe(false);
    expect(isOwner("owner")).toBe(true);
  });
});

describe("navigation filtering", () => {
  it("hides chat and admin tools from viewers", () => {
    const items = visibleNavItems({ role: "viewer", is_platform_admin: false });
    expect(items.map((item) => item.to)).toEqual([
      "/profiles",
      "/projects",
      "/calendar",
      "/publish",
      "/analytics",
    ]);
  });

  it("shows chat and accounts to editors but not admin tools", () => {
    const items = visibleNavItems({ role: "editor", is_platform_admin: false });
    expect(items.map((item) => item.to)).toContain("/chat");
    expect(items.map((item) => item.to)).toContain("/accounts");
    expect(items.map((item) => item.to)).not.toContain("/settings");
    expect(items.map((item) => item.to)).not.toContain("/team");
  });

  it("shows admin tools to admins and owner-only tools to owners", () => {
    const admin = visibleNavItems({ role: "admin", is_platform_admin: false });
    expect(admin.map((item) => item.to)).toContain("/audit");
    expect(admin.map((item) => item.to)).not.toContain("/tenants");
    const owner = visibleNavItems({ role: "owner", is_platform_admin: false });
    expect(owner.map((item) => item.to)).toContain("/team");
    expect(owner.map((item) => item.to)).not.toContain("/tenants");
  });

  it("shows tenants only to platform admins", () => {
    const platform = visibleNavItems({ role: "owner", is_platform_admin: true });
    expect(platform.map((item) => item.to)).toContain("/tenants");
    const viewerPlatform = visibleNavItems({
      role: "viewer",
      is_platform_admin: true,
    });
    expect(viewerPlatform.map((item) => item.to)).toContain("/tenants");
  });
});

describe("post-login routing", () => {
  it("forces first-login password change", () => {
    expect(
      mustChangePath({ must_change_password: true }, "/projects"),
    ).toBe("/change-password");
    expect(
      mustChangePath({ must_change_password: true }, "/change-password"),
    ).toBeNull();
    expect(
      mustChangePath({ must_change_password: false }, "/projects"),
    ).toBeNull();
  });

  it("sends viewers to projects and writers to chat", () => {
    expect(defaultPath({ role: "viewer" })).toBe("/projects");
    expect(defaultPath({ role: "editor" })).toBe("/chat");
    expect(defaultPath({ role: "owner" })).toBe("/chat");
  });
});
