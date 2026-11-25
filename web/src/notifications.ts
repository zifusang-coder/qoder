export type NotificationKind = "approval" | "review" | "failure";
export type NotificationSeverity = "warn" | "error";

export interface AppNotification {
  id: string;
  kind: NotificationKind;
  source: string;
  severity: NotificationSeverity;
  title: string;
  detail: string;
  href: string;
  created_at: string | null;
}

export interface NotificationFeed {
  items: AppNotification[];
  counts: Record<NotificationKind, number>;
}

export const SEEN_STORAGE_KEY = "newmedia.notifications.seen";

export const EMPTY_FEED: NotificationFeed = {
  items: [],
  counts: { approval: 0, review: 0, failure: 0 },
};

const SOURCE_LABELS: Record<string, string> = {
  assistant: "AI 助理",
  workflow: "工作流",
  publish: "发布",
  media: "媒体",
};

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

export function severityDot(severity: string): string {
  return severity === "error" ? "bg-coral-500" : "bg-honey-600";
}

function parseTime(value: string | null): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

export function unseenCount(
  items: AppNotification[],
  seenAt: string | null,
): number {
  const seen = parseTime(seenAt);
  return items.filter((item) => {
    if (!item.created_at) return true;
    const time = parseTime(item.created_at);
    if (time === null) return false;
    return seen === null || time > seen;
  }).length;
}

export function notificationAge(
  createdAt: string | null,
  now: Date = new Date(),
): string {
  const time = parseTime(createdAt);
  if (time === null) return "";
  const diff = Math.max(0, now.getTime() - time);
  if (diff < MINUTE_MS) return "刚刚";
  if (diff < HOUR_MS) return `${Math.floor(diff / MINUTE_MS)} 分钟前`;
  if (diff < DAY_MS) return `${Math.floor(diff / HOUR_MS)} 小时前`;
  if (diff < 7 * DAY_MS) return `${Math.floor(diff / DAY_MS)} 天前`;
  const date = new Date(time);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

export function readSeenAt(storage: ReadableStorage): string | null {
  try {
    return storage.getItem(SEEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeSeenAt(storage: WritableStorage, at: string): void {
  try {
    storage.setItem(SEEN_STORAGE_KEY, at);
  } catch {
    // 隐私模式下写入受限，通知看板仍可用，只是徽标不会清零。
  }
}
