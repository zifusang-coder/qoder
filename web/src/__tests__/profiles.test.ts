import { describe, expect, it } from "vitest";
import { resolveProfileTarget } from "../profiles";

const ITEMS = [
  { id: "p1", name: "主账号" },
  { id: "p2", name: "副账号" },
];

describe("resolveProfileTarget", () => {
  it("returns the selected profile", () => {
    expect(resolveProfileTarget(ITEMS, "p2")?.name).toBe("副账号");
  });

  it("falls back to the first profile when the selection is stale", () => {
    expect(resolveProfileTarget(ITEMS, "deleted")?.id).toBe("p1");
    expect(resolveProfileTarget(ITEMS, "")?.id).toBe("p1");
  });

  it("returns undefined when there are no profiles", () => {
    expect(resolveProfileTarget([], "p1")).toBeUndefined();
  });
});
