import { describe, expect, it } from "vitest";
import { actionFor, type Job, type JobActionKind } from "../types";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    content_id: "c1",
    content_version: 1,
    platform: "wechat",
    account_id: null,
    status: "draft",
    scheduled_at: null,
    published_at: null,
    external_id: "",
    external_url: "",
    export_path: "",
    attempts: 0,
    error: "",
    checks: {},
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

type Expected = { action: JobActionKind; label: string } | null;

describe("actionFor", () => {
  it.each<[string, string, Expected]>([
    ["draft", "wechat", { action: "precheck", label: "发布前检查" }],
    ["precheck_failed", "wechat", { action: "precheck", label: "发布前检查" }],
    ["awaiting_review", "wechat", { action: "approve", label: "人工通过" }],
    ["approved", "wechat", { action: "execute", label: "发布到公众号草稿" }],
    ["approved", "xiaohongshu", { action: "export", label: "导出素材包" }],
    ["approved", "zhihu", { action: "export", label: "导出素材包" }],
    ["approved", "kuaishou", { action: "export", label: "导出素材包" }],
    ["approved", "channels", { action: "export", label: "导出素材包" }],
    ["approved", "douyin", null],
    ["approved", "bilibili", null],
    ["published", "wechat", null],
    ["approved", "unknown", null],
  ])("status %s on %s offers %o", (status, platform, expected) => {
    expect(actionFor(job({ status, platform }))).toEqual(expected);
  });
});
