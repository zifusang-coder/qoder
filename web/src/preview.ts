export type PreviewLayout = "article" | "video";

export interface PreviewStyle {
  layout: PreviewLayout;
  frame: string;
  header: string;
  accent: string;
  title: string;
}

export const PREVIEW_PLATFORMS = [
  "wechat",
  "douyin",
  "toutiao",
  "baijiahao",
] as const;

const STYLES: Record<string, PreviewStyle> = {
  wechat: {
    layout: "article",
    frame: "border-mint-100 bg-white",
    header: "bg-mint-50 text-mint-700",
    accent: "text-mint-700",
    title: "font-semibold text-ink-900",
  },
  douyin: {
    layout: "video",
    frame: "border-ink-900/20 bg-ink-900",
    header: "bg-ink-900 text-white",
    accent: "text-white",
    title: "font-semibold text-white",
  },
  toutiao: {
    layout: "article",
    frame: "border-coral-100 bg-white",
    header: "bg-coral-50 text-coral-600",
    accent: "text-coral-600",
    title: "font-semibold text-ink-900",
  },
  baijiahao: {
    layout: "article",
    frame: "border-brand-100 bg-white",
    header: "bg-brand-50 text-brand-700",
    accent: "text-brand-700",
    title: "font-semibold text-ink-900",
  },
};

const DEFAULT_STYLE: PreviewStyle = {
  layout: "article",
  frame: "border-brand-100 bg-white",
  header: "bg-brand-50 text-brand-700",
  accent: "text-brand-700",
  title: "font-semibold text-ink-900",
};

export function previewStyle(platform: string): PreviewStyle {
  return STYLES[platform] ?? DEFAULT_STYLE;
}

export function previewTitle(title: string, platformLabel: string): string {
  const trimmed = title.trim();
  return trimmed || `${platformLabel} · 未命名内容`;
}
