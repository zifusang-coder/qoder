import { describe, expect, it } from "vitest";
import {
  actionFor,
  canCancel,
  canReject,
  canReschedule,
  type Job,
  type JobActionKind,
} from "../types";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    content_id: "c1",
    content_version: 1,
    content_kind: "article",
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
  it.each<[string, string, string, Expected]>([
    ["draft", "wechat", "article", { action: "precheck", label: "发布前检查" }],
    [
      "precheck_failed",
      "wechat",
      "article",
      { action: "precheck", label: "发布前检查" },
    ],
    [
      "awaiting_review",
      "wechat",
      "article",
      { action: "approve", label: "人工通过" },
    ],
    [
      "approved",
      "wechat",
      "article",
      { action: "execute", label: "发布到公众号草稿" },
    ],
    [
      "approved",
      "douyin",
      "video",
      { action: "execute", label: "发布到抖音" },
    ],
    [
      "approved",
      "toutiao",
      "video",
      { action: "execute", label: "发布到头条" },
    ],
    ["approved", "toutiao", "article", null],
    ["approved", "baijiahao", "article", null],
    ["approved", "bilibili", "video", null],
    [
      "approved",
      "xiaohongshu",
      "note",
      { action: "export", label: "导出素材包" },
    ],
    ["approved", "zhihu", "article", { action: "export", label: "导出素材包" }],
    [
      "approved",
      "kuaishou",
      "video",
      { action: "export", label: "导出素材包" },
    ],
    [
      "approved",
      "channels",
      "article",
      { action: "export", label: "导出素材包" },
    ],
    [
      "export_failed",
      "xiaohongshu",
      "note",
      { action: "retry_export", label: "重试导出" },
    ],
    ["export_failed", "wechat", "article", null],
    [
      "publish_failed",
      "wechat",
      "article",
      { action: "retry", label: "重试发布" },
    ],
    [
      "publish_failed",
      "douyin",
      "video",
      { action: "retry", label: "重试发布" },
    ],
    ["publish_failed", "xiaohongshu", "note", null],
    ["published", "wechat", "article", null],
    ["cancelled", "wechat", "article", null],
    ["exported", "xiaohongshu", "note", null],
    ["exporting", "xiaohongshu", "note", null],
    ["publishing", "wechat", "article", null],
    ["approved", "unknown", "article", null],
  ])(
    "status %s on %s/%s offers %o",
    (status, platform, contentKind, expected) => {
      expect(
        actionFor(job({ status, platform, content_kind: contentKind })),
      ).toEqual(expected);
    },
  );
});

describe("canCancel", () => {
  it.each<[string, boolean]>([
    ["draft", true],
    ["precheck_failed", true],
    ["awaiting_review", true],
    ["approved", true],
    ["export_failed", true],
    ["publish_failed", true],
    ["prechecking", false],
    ["exporting", false],
    ["exported", false],
    ["publishing", false],
    ["published", false],
    ["cancelled", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canCancel(job({ status }))).toBe(expected);
  });
});

describe("canReject", () => {
  it.each<[string, boolean]>([
    ["awaiting_review", true],
    ["draft", false],
    ["approved", false],
    ["published", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canReject(job({ status }))).toBe(expected);
  });
});

describe("canReschedule", () => {
  it.each<[string, boolean]>([
    ["draft", true],
    ["prechecking", true],
    ["precheck_failed", true],
    ["awaiting_review", true],
    ["approved", true],
    ["exporting", false],
    ["exported", false],
    ["publishing", false],
    ["published", false],
    ["cancelled", false],
  ])("status %s -> %s", (status, expected) => {
    expect(canReschedule(job({ status }))).toBe(expected);
  });
});
