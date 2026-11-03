export interface Profile {
  id: string;
  name: string;
  account_name: string;
  positioning: string;
  audience: string;
  tone: string;
  taboos: string[];
  goals: string[];
  platform_focus: string[];
  is_active: boolean;
}

export interface ProfileMemory {
  id: string;
  content: string;
  kind: string;
  weight: number;
  source: string;
  tags: string[];
}

export interface Account {
  id: string;
  platform: string;
  name: string;
  status: string;
  credential_masked: string;
}

export interface Project {
  id: string;
  title: string;
  topic: string;
  status: string;
  profile_id: string | null;
  platforms: string[];
}

export interface Content {
  id: string;
  project_id: string;
  platform: string;
  kind: string;
  title: string;
  current_version: number;
  status: string;
}

export interface Version {
  id: string;
  content_id: string;
  version: number;
  title: string;
  body: string;
  source: string;
}

export interface RepurposeResult extends Version {
  platform: string;
}

export interface PrecheckIssue {
  level: string;
  message: string;
}

export interface JobChecks {
  issues?: PrecheckIssue[];
  passed?: boolean;
}

export interface Job {
  id: string;
  content_id: string;
  content_version: number;
  platform: string;
  account_id: string | null;
  status: string;
  scheduled_at: string | null;
  published_at: string | null;
  external_id: string;
  external_url: string;
  export_path: string;
  attempts: number;
  error: string;
  checks: JobChecks;
  created_at: string;
  updated_at: string;
}

export interface ProfileList {
  items: Profile[];
}

export interface ProjectList {
  items: Project[];
}

export interface VersionList {
  items: Version[];
}

export interface JobList {
  items: Job[];
  total: number;
}

export interface PrecheckResult {
  job: Job;
  passed: boolean;
  errors: number;
  warnings: number;
  issues: PrecheckIssue[];
}

export interface AnalyticsItem {
  content_id: string;
  title: string;
  platform: string;
  has_data: boolean;
  captured_at: string | null;
  views: number;
  likes: number;
  comments: number;
  shares: number;
  saves: number;
  followers_delta: number;
}

export interface AnalyticsTotals {
  views: number;
  likes: number;
  comments: number;
  shares: number;
  saves: number;
  followers_delta: number;
}

export interface AnalyticsOverview {
  items: AnalyticsItem[];
  totals: AnalyticsTotals;
  engagement_rate: number;
}

export interface ImportResult {
  upserted: number;
  errors: string[];
}

export interface PostmortemResult {
  report: Record<string, unknown>;
  memory_ids: string[];
  run_id: string;
}

export interface ChatOut {
  reply: string;
  intent: string;
  skill_id?: string | null;
  run_id?: string | null;
  data?: Record<string, unknown> | null;
  missing?: string[] | null;
}

export const PLATFORMS = [
  ["wechat", "公众号"],
  ["xiaohongshu", "小红书"],
  ["zhihu", "知乎"],
  ["kuaishou", "快手"],
  ["channels", "视频号"],
  ["douyin", "抖音"],
  ["bilibili", "B站"],
] as const;

/** Platforms whose approved jobs are delivered through manual export. */
export const MANUAL_EXPORT_PLATFORMS = [
  "xiaohongshu",
  "zhihu",
  "kuaishou",
  "channels",
] as const;

export function platformLabel(platform: string): string {
  return PLATFORMS.find(([value]) => value === platform)?.[1] ?? platform;
}

export function isManualExportPlatform(platform: string): boolean {
  return MANUAL_EXPORT_PLATFORMS.some((value) => value === platform);
}

export type JobActionKind =
  | "precheck"
  | "approve"
  | "reject"
  | "cancel"
  | "execute"
  | "export"
  | "retry"
  | "retry_export";

const CANCELLABLE_STATUSES = new Set([
  "draft",
  "precheck_failed",
  "awaiting_review",
  "approved",
  "export_failed",
  "publish_failed",
]);

const RESCHEDULABLE_STATUSES = new Set([
  "draft",
  "prechecking",
  "precheck_failed",
  "awaiting_review",
  "approved",
]);

export function canCancel(job: Job): boolean {
  return CANCELLABLE_STATUSES.has(job.status);
}

export function canReject(job: Job): boolean {
  return job.status === "awaiting_review";
}

export function canReschedule(job: Job): boolean {
  return RESCHEDULABLE_STATUSES.has(job.status);
}

export function actionFor(
  job: Job,
): { action: JobActionKind; label: string } | null {
  if (job.status === "draft" || job.status === "precheck_failed") {
    return { action: "precheck", label: "发布前检查" };
  }
  if (job.status === "awaiting_review") {
    return { action: "approve", label: "人工通过" };
  }
  if (job.status === "approved") {
    if (job.platform === "wechat") {
      return { action: "execute", label: "发布到公众号草稿" };
    }
    if (isManualExportPlatform(job.platform)) {
      return { action: "export", label: "导出素材包" };
    }
  }
  if (job.status === "export_failed") {
    // 导出失败必须先经 actions 的 retry 回到 exporting，再调用 export
    return isManualExportPlatform(job.platform)
      ? { action: "retry_export", label: "重试导出" }
      : null;
  }
  if (job.status === "publish_failed") {
    return job.platform === "wechat"
      ? { action: "retry", label: "重试发布" }
      : null;
  }
  return null;
}

export const JOB_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  prechecking: "检查中",
  precheck_failed: "检查未通过",
  awaiting_review: "待人工审核",
  approved: "已通过审核",
  exporting: "导出中",
  exported: "已导出",
  export_failed: "导出失败",
  publishing: "发布中",
  published: "已发布",
  publish_failed: "发布失败",
  cancelled: "已取消",
};

export function jobStatusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? status;
}

export const PROJECT_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  active: "进行中",
  archived: "已归档",
};

export function projectStatusLabel(status: string): string {
  return PROJECT_STATUS_LABELS[status] ?? status;
}

export const CONTENT_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  published: "已发布",
  archived: "已归档",
};

export function contentStatusLabel(status: string): string {
  return CONTENT_STATUS_LABELS[status] ?? status;
}

export const CONTENT_KIND_LABELS: Record<string, string> = {
  article: "文章",
  note: "笔记",
  video: "视频",
};

export function contentKindLabel(kind: string): string {
  return CONTENT_KIND_LABELS[kind] ?? kind;
}

export function errorText(err: unknown): string {
  if (!(err instanceof Error) || !err.message) return "请求失败";
  const detail: unknown = "detail" in err ? err.detail : undefined;
  const detailItems: unknown[] = Array.isArray(detail) ? detail : [];
  const messages = detailItems.flatMap((item) => {
    if (typeof item !== "object" || item === null || !("msg" in item)) {
      return [];
    }
    const message = item.msg;
    return typeof message === "string" && message.trim() ? [message] : [];
  });
  if (messages.length > 0) return `${err.message}：${messages.join("；")}`;
  return err.message;
}
