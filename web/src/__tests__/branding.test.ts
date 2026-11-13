import { describe, expect, it } from "vitest";
import html from "../../index.html?raw";
import login from "../pages/Login.tsx?raw";
import layout from "../components/Layout.tsx?raw";

describe("品牌名", () => {
  it("文档标题使用新名称", () => {
    expect(html).toContain("<title>云AI新媒体运营系统</title>");
  });

  it("登录页与顶栏使用统一常量", () => {
    expect(login).toContain("APP_NAME");
    expect(layout).toContain("APP_NAME");
    expect(login).not.toContain(">新媒体运营系统<");
    expect(layout).not.toContain(">新媒体运营<");
  });
});
