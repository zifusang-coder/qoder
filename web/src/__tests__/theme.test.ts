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
  "--color-paper: #eef3ff;",
  "--color-surface: #ffffff;",
  "--color-ink-900: #1f2547;",
  "--color-ink-700: #3f4770;",
  "--color-ink-500: #6f78a3;",
  "--color-ink-300: #aeb5d6;",
  "--color-line: #e4e8fa;",
  "--color-brand-50: #f2efff;",
  "--color-brand-100: #e5e0ff;",
  "--color-brand-500: #7b6cf6;",
  "--color-brand-600: #6a54ef;",
  "--color-brand-700: #5540d4;",
  "--color-mint-50: #e9faf2;",
  "--color-mint-100: #d2f4e4;",
  "--color-mint-700: #188a5b;",
  "--color-coral-50: #fff1ee;",
  "--color-coral-100: #ffdfd8;",
  "--color-coral-600: #e25848;",
  "--color-coral-700: #c4463a;",
  "--color-honey-50: #fff6e6;",
  "--color-honey-100: #ffedd0;",
  "--color-honey-700: #b37210;",
];

const COMPONENT_CLASSES = [
  ".card {",
  ".card-pad {",
  ".input {",
  ".btn-primary {",
  ".btn-secondary {",
  ".btn-danger {",
  ".badge {",
  ".brand-mark {",
  ".nav-link {",
  ".nav-link-active {",
];

describe("theme tokens", () => {
  it("defines the pastel paper/brand palette", () => {
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
    expect(layout).toContain("backdrop-blur-md");
    expect(layout).toContain("nav-link-active");
    expect(layout).toContain("text-brand-700");

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
