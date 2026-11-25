import { describe, expect, it } from "vitest";
import { PREVIEW_PLATFORMS, previewStyle, previewTitle } from "../preview";

describe("previewStyle", () => {
  it("renders wechat as a mint article preview", () => {
    const style = previewStyle("wechat");
    expect(style.layout).toBe("article");
    expect(style.header).toContain("mint");
    expect(style.accent).toContain("mint");
  });

  it("renders douyin as a dark vertical video preview", () => {
    const style = previewStyle("douyin");
    expect(style.layout).toBe("video");
    expect(style.frame).toContain("ink-900");
  });

  it("renders toutiao and baijiahao as article previews", () => {
    expect(previewStyle("toutiao").layout).toBe("article");
    expect(previewStyle("baijiahao").layout).toBe("article");
    expect(previewStyle("toutiao").header).not.toBe(
      previewStyle("baijiahao").header,
    );
  });

  it("falls back to brand article styling for unknown platforms", () => {
    const style = previewStyle("unknown-platform");
    expect(style.layout).toBe("article");
    expect(style.header).toContain("brand");
  });

  it("lists the four featured preview platforms", () => {
    expect(PREVIEW_PLATFORMS).toEqual([
      "wechat",
      "douyin",
      "toutiao",
      "baijiahao",
    ]);
  });
});

describe("previewTitle", () => {
  it("prefers the content title", () => {
    expect(previewTitle("公众号标题", "公众号")).toBe("公众号标题");
  });

  it("falls back to the platform label when the title is blank", () => {
    expect(previewTitle("   ", "公众号")).toBe("公众号 · 未命名内容");
  });
});
