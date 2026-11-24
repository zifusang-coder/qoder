import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    css: true,
    // e2e/ 是 Playwright 用例，由 `npm run test:e2e` 运行；vitest 默认 include 会误收集
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "e2e/**",
    ],
  },
});
