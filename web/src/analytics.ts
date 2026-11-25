export interface ChannelInsight {
  platform: string;
  contents: number;
  views: number;
  interactions: number;
  engagement_rate: number;
}

export interface ContentInsight {
  content_id: string;
  title: string;
  platform: string;
  views: number;
  interactions: number;
  engagement_rate: number;
}

export interface TimeBucket {
  weekday: number;
  hour: number;
  samples: number;
  avg_views: number;
  avg_interactions: number;
  engagement_rate: number;
}

export interface BestSlot {
  weekday: number | null;
  hour: number;
  samples: number;
  avg_views: number;
  avg_interactions: number;
  source: "history" | "fallback";
}

export interface AnalyticsInsights {
  channels: ChannelInsight[];
  contents: ContentInsight[];
  time_buckets: TimeBucket[];
  best_slots: BestSlot[];
  has_time_data: boolean;
  published_contents: number;
}

export const WEEKDAY_LABELS = [
  "周一",
  "周二",
  "周三",
  "周四",
  "周五",
  "周六",
  "周日",
] as const;

export function weekdayLabel(weekday: number | null): string {
  if (weekday === null) return "每天";
  return WEEKDAY_LABELS[weekday] ?? "每天";
}

export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

export function slotLabel(weekday: number | null, hour: number): string {
  return `${weekdayLabel(weekday)} ${hourLabel(hour)}`;
}

export function engagementPercent(rate: number): string {
  return `${(rate * 100).toFixed(2)}%`;
}

export function sharePercent(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((value / max) * 100)));
}

export function rankTimeBuckets(
  buckets: TimeBucket[],
  limit = 8,
): TimeBucket[] {
  return [...buckets]
    .sort(
      (a, b) =>
        b.avg_interactions - a.avg_interactions ||
        b.avg_views - a.avg_views ||
        b.samples - a.samples ||
        a.weekday - b.weekday ||
        a.hour - b.hour,
    )
    .slice(0, limit);
}
