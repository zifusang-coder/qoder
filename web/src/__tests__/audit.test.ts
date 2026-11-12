import { describe, expect, it } from "vitest";
import {
  auditActionLabel,
  auditStatusLabel,
  buildAuditQuery,
} from "../audit";

describe("buildAuditQuery", () => {
  it("omits empty filters and keeps pagination", () => {
    expect(
      buildAuditQuery(
        {
          action: "auth.login",
          status: "",
          actor_email: "  ",
          from: "",
          to: "",
        },
        20,
        40,
      ),
    ).toBe("action=auth.login&limit=20&offset=40");
  });

  it("sends the time range as UTC ISO strings", () => {
    const query = buildAuditQuery({
      action: "",
      status: "failure",
      actor_email: "a@local",
      from: "2026-11-01T08:00",
      to: "2026-11-02T08:00",
    });
    const params = new URLSearchParams(query);
    expect(params.get("status")).toBe("failure");
    expect(params.get("actor_email")).toBe("a@local");
    expect(params.get("from")).toBe(new Date("2026-11-01T08:00").toISOString());
    expect(params.get("to")).toBe(new Date("2026-11-02T08:00").toISOString());
    expect(params.get("limit")).toBe("50");
    expect(params.get("offset")).toBe("0");
  });
});

describe("audit labels", () => {
  it("labels known actions and falls back to the raw code", () => {
    expect(auditActionLabel("auth.login")).toBe("登录成功");
    expect(auditActionLabel("publish.auto_execute")).toBe("定时自动发布");
    expect(auditActionLabel("custom.action")).toBe("custom.action");
  });

  it("labels status", () => {
    expect(auditStatusLabel("success")).toBe("成功");
    expect(auditStatusLabel("failure")).toBe("失败");
    expect(auditStatusLabel("other")).toBe("other");
  });
});
