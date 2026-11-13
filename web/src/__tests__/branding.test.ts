import { describe, expect, it } from "vitest";
import html from "../../index.html?raw";
import login from "../pages/Login.tsx?raw";
import layout from "../components/Layout.tsx?raw";
import { APP_NAME } from "../branding";

describe("品牌名", () => {
  it("文档标题使用新名称", () => {
    expect(APP_NAME).toBe("云AI新媒体运营系统");
    expect(html).toContain(`<title>${APP_NAME}</title>`);
  });

  it("登录页与顶栏使用统一常量", () => {
    expect(login).toMatch(/\{APP_NAME\}/);
    expect(layout).toMatch(/\{APP_NAME\}/);
    expect(login).not.toContain("新媒体运营");
    expect(layout).not.toContain("新媒体运营");
  });
});
