import { describe, expect, it } from "vitest";
import { NAV_ITEMS, visibleNavItems, type Me } from "../auth";
import { NAV_GROUP_LABELS, groupedNav } from "../nav";

function me(overrides: Partial<Me>): Pick<Me, "role" | "is_platform_admin"> {
  return { role: "viewer", is_platform_admin: false, ...overrides };
}

describe("groupedNav", () => {
  it("groups every visible item exactly once", () => {
    const items = visibleNavItems(me({ role: "owner", is_platform_admin: true }));
    const groups = groupedNav(items);
    const flat = groups.flatMap((group) => group.items);
    expect(flat).toHaveLength(items.length);
    expect(new Set(flat.map((item) => item.to)).size).toBe(items.length);
  });

  it("keeps the declaration order inside each group", () => {
    const groups = groupedNav(visibleNavItems(me({ role: "owner" })));
    const ai = groups.find((group) => group.key === "ai");
    expect(ai?.items.map((item) => item.to)).toEqual([
      "/assistant",
      "/workflows",
      "/chat",
    ]);
    const manage = groups.find((group) => group.key === "manage");
    expect(manage?.items.map((item) => item.to)).toEqual([
      "/settings",
      "/team",
      "/audit",
    ]);
  });

  it("drops empty groups for limited roles", () => {
    const groups = groupedNav(visibleNavItems(me({ role: "viewer" })));
    expect(groups.map((group) => group.key)).toEqual(["content"]);
    expect(groups[0].label).toBe(NAV_GROUP_LABELS.content);
  });

  it("puts tenant management in the manage group for platform admins", () => {
    const groups = groupedNav(
      visibleNavItems(me({ role: "viewer", is_platform_admin: true })),
    );
    const manage = groups.find((group) => group.key === "manage");
    expect(manage?.items.map((item) => item.to)).toEqual(["/tenants"]);
  });

  it("returns no groups for an empty nav", () => {
    expect(groupedNav([])).toEqual([]);
  });
});

describe("nav metadata", () => {
  it("gives every nav item an icon and a known group", () => {
    const known = Object.keys(NAV_GROUP_LABELS);
    for (const item of NAV_ITEMS) {
      expect(item.icon).toBeTruthy();
      expect(known).toContain(item.group);
    }
  });
});
