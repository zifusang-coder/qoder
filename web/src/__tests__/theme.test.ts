import { describe, expect, it } from "vitest";
import css from "../index.css?raw";

const sources = import.meta.glob(
  ["../**/*.{ts,tsx,css}", "!../__tests__/**"],
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

function source(path: string): string {
  const key = Object.keys(sources).find((item) => item.endsWith(path));
  if (!key) throw new Error(`未找到源文件：${path}`);
  return sources[key];
}

const TOKENS = [
  "--color-paper: #f6f6f3;",
  "--color-surface: #ffffff;",
  "--color-ink-900: #1c211e;",
  "--color-ink-700: #3d4642;",
  "--color-ink-500: #6b7570;",
  "--color-ink-300: #a8b0ac;",
  "--color-line: #e3e5e1;",
  "--color-sage-50: #eef5f0;",
  "--color-sage-100: #dcebe1;",
  "--color-sage-500: #3f8a66;",
  "--color-sage-600: #2f7554;",
  "--color-sage-700: #245c43;",
  "--color-wine-50: #f8eef0;",
  "--color-wine-100: #efd9dd;",
  "--color-wine-500: #8c3a4a;",
  "--color-wine-600: #752f3d;",
  "--color-wine-700: #5d2531;",
  "--color-warm-600: #8a6a3b;",
];

const COMPONENT_CLASSES = [
  ".card {",
  ".card-pad {",
  ".input {",
  ".btn-primary {",
  ".btn-secondary {",
  ".btn-danger {",
  ".badge {",
  ".nav-link {",
  ".nav-link-active {",
];

describe("theme tokens", () => {
  it("defines the paper/sage/wine palette", () => {
    for (const token of TOKENS) {
      expect(css).toContain(token);
    }
  });

  it("applies the light base and component classes", () => {
    expect(css).toContain("bg-paper text-ink-900");
    for (const className of COMPONENT_CLASSES) {
      expect(css).toContain(className);
    }
  });
});

describe("application shell", () => {
  it("uses the clean component classes", () => {
    const layout = source("/components/Layout.tsx");
    expect(layout).toContain("border-line bg-surface");
    expect(layout).toContain("nav-link-active");
    expect(layout).toContain("text-sage-700");

    const login = source("/pages/Login.tsx");
    expect(login).toContain("btn-primary");
    expect(login).toContain('className="input"');
    expect(login).not.toMatch(/slate-|indigo-/);
  });
});

describe("legacy palette", () => {
  it("has no old dark-theme color utilities", () => {
    const legacy =
      /(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}/;
    const offenders = Object.entries(sources)
      .filter(([, text]) => legacy.test(text))
      .map(([path]) => path)
      .sort();
    expect(offenders).toEqual([]);
  });
});
