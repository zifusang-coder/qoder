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
