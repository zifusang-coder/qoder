import { describe, expect, it } from "vitest";
import {
  CAPABILITY_LABELS,
  FALLBACK_PLATFORMS,
  accountsForPlatform,
  capabilityFor,
  specFor,
} from "../platforms";

describe("平台规格", () => {
  it("内置 9 个平台并按固定顺序排列", () => {
    expect(FALLBACK_PLATFORMS.map((spec) => spec.key)).toEqual([
      "wechat",
      "douyin",
      "toutiao",
      "xiaohongshu",
      "zhihu",
      "kuaishou",
      "channels",
      "baijiahao",
      "bilibili",
    ]);
    expect(specFor("toutiao")?.label).toBe("头条");
    expect(specFor("unknown")).toBeUndefined();
  });

  it("能力按内容类型查询，未知类型为待接入", () => {
    const toutiao = specFor("toutiao")!;
    expect(capabilityFor(toutiao, "video")).toBe("auto_publish");
    expect(capabilityFor(toutiao, "article")).toBe("pending");
    expect(capabilityFor(toutiao, "audio")).toBe("pending");
  });

  it("账号按平台与启用状态过滤", () => {
    const accounts = [
      { id: "1", platform: "wechat", status: "active" },
      { id: "2", platform: "wechat", status: "revoked" },
      { id: "3", platform: "douyin", status: "active" },
    ];
    expect(accountsForPlatform(accounts, "wechat").map((a) => a.id)).toEqual([
      "1",
    ]);
    expect(accountsForPlatform(accounts, "douyin").map((a) => a.id)).toEqual([
      "3",
    ]);
  });

  it("能力文案齐全", () => {
    expect(CAPABILITY_LABELS.auto_publish).toBe("自动发布");
    expect(CAPABILITY_LABELS.manual_export).toBe("人工导出");
    expect(CAPABILITY_LABELS.pending).toBe("待接入");
  });
});
