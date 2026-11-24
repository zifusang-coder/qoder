import { expect, test, type Page } from "@playwright/test";

const suffix = Date.now().toString(36);

test.describe.configure({ mode: "serial" });

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill(process.env.E2E_EMAIL ?? "admin@local");
  await page.getByLabel("密码").fill(process.env.E2E_PASSWORD ?? "admin12345");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "运营助手" })).toBeVisible();
}

test("登录后进入助手首页并展示主导航", async ({ page }) => {
  await login(page);
  for (const label of ["AI 助理", "工作流", "账号画像", "项目"]) {
    await expect(page.getByRole("link", { name: label, exact: true })).toBeVisible();
  }
});

test("创建账号画像", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "账号画像", exact: true }).click();
  await expect(page.getByRole("heading", { name: "账号画像" })).toBeVisible();
  const name = `冒烟画像-${suffix}`;
  await page.getByPlaceholder("例如：产品经理小林").fill(name);
  await page.getByRole("button", { name: "创建画像" }).click();
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
});

test("创建项目", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "项目", exact: true }).click();
  await expect(page.getByRole("heading", { name: "项目", exact: true })).toBeVisible();
  const title = `冒烟项目-${suffix}`;
  await page.getByPlaceholder("例如：AI 写作工作流").fill(title);
  await page.getByRole("button", { name: "创建项目" }).click();
  await expect(page.getByText(`已创建项目「${title}」`)).toBeVisible();
  await expect(page.getByRole("link", { name: "打开项目" })).toBeVisible();
});

test("助手会话页可输入", async ({ page }) => {
  await login(page);
  await expect(page.getByRole("button", { name: "新对话" })).toBeVisible();
  await expect(page.getByLabel("对话输入")).toBeVisible();
});

test("工作流与 AI 助理入口可用", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "工作流", exact: true }).click();
  await expect(page.getByRole("heading", { name: "工作流", exact: true })).toBeVisible();
  await expect(page.getByText("新建工作流")).toBeVisible();
  await page.getByRole("link", { name: "AI 助理", exact: true }).click();
  await expect(page.getByRole("heading", { name: "AI 助理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "单 Agent" })).toBeVisible();
  await expect(page.getByRole("button", { name: "协作团队" })).toBeVisible();
});
