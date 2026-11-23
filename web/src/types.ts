import {
  FALLBACK_PLATFORMS,
  capabilityFor,
  specFor,
  type Capability,
  type PlatformSpec,
} from "./platforms";

export type { Me } from "./auth";

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
  meta: Record<string, unknown>;
  capability: string;
  capability_note: string;
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
  content_kind: string;
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

export interface CalendarItem {
  job_id: string;
  content_id: string;
  project_id: string;
  project_title: string;
  content_title: string;
  platform: string;
  status: string;
  scheduled_at: string | null;
  published_at: string | null;
  account_name: string | null;
}

export interface CalendarOut {
  items: CalendarItem[];
}

export interface Member {
  id: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface MemberCreated extends Member {
  initial_password: string;
}

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  status: string;
  members_count: number;
  created_at: string;
}

export interface TenantCreated extends Tenant {
  owner: MemberCreated;
}

export interface ChatOut {
  reply: string;
  intent: string;
  skill_id?: string | null;
  run_id?: string | null;
  data?: Record<string, unknown> | null;
  missing?: string[] | null;
  conversation_id?: string | null;
}

export interface ChatConversation {
  id: string;
  title: string;
  profile_id?: string | null;
  updated_at: string;
}

export interface ChatConversationList {
  items: ChatConversation[];
}

export interface ChatMessageItem {
  id: string;
  role: "user" | "assistant";
  content: string;
  intent?: string | null;
  skill_id?: string | null;
  run_id?: string | null;
  data?: Record<string, unknown> | null;
  missing?: string[] | null;
  created_at: string;
}

export interface ChatConversationDetail extends ChatConversation {
  messages: ChatMessageItem[];
}

export interface MediaFile {
  name: string;
  path: string;
  kind: string;
  size: number;
}

export interface MediaItem {
  run_id: string;
  content_id: string;
  project_id: string;
  version_id: string;
  version: number;
  platform: string;
  kind: string;
  title: string;
  body: string;
  duration_seconds: number;
  files: MediaFile[];
  preview: string | null;
  cover: string | null;
  subtitles: string | null;
  audio: {
    status?: string;
    voice?: string;
    llm_fallback?: string;
  };
  created_at: string;
}

export interface MediaList {
  items: MediaItem[];
}

export interface MediaJob {
  id: string;
  project_id: string;
  skill_id: string;
  status: string;
  content_id: string | null;
  error_code: string;
  error_message: string;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditLog {
  id: string;
  actor_email: string;
  action: string;
  resource_type: string;
  resource_id: string;
  status: string;
  ip: string;
  user_agent: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface AuditLogList {
  items: AuditLog[];
  total: number;
}

export const PLATFORMS = [
  ["wechat", "公众号"],
  ["douyin", "抖音"],
  ["toutiao", "头条"],
  ["xiaohongshu", "小红书"],
  ["zhihu", "知乎"],
  ["kuaishou", "快手"],
  ["channels", "视频号"],
  ["baijiahao", "百家号"],
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

const EXECUTE_LABELS: Record<string, string> = {
  wechat: "发布到公众号草稿",
  douyin: "发布到抖音",
  toutiao: "发布到头条",
};

export function actionFor(
  job: Pick<Job, "status" | "platform" | "content_kind">,
  specs: PlatformSpec[] = FALLBACK_PLATFORMS,
): { action: JobActionKind; label: string } | null {
  if (job.status === "draft" || job.status === "precheck_failed") {
    return { action: "precheck", label: "发布前检查" };
  }
  if (job.status === "awaiting_review") {
    return { action: "approve", label: "人工通过" };
  }

  const spec = specFor(job.platform, specs);
  const capability: Capability = spec
    ? capabilityFor(spec, job.content_kind)
    : "pending";

  if (job.status === "approved") {
    if (capability === "auto_publish") {
      return {
        action: "execute",
        label:
          EXECUTE_LABELS[job.platform] ??
          `发布到${spec?.label ?? job.platform}`,
      };
    }
    if (capability === "manual_export") {
      return { action: "export", label: "导出素材包" };
    }
  }
  if (job.status === "export_failed" && capability === "manual_export") {
    return { action: "retry_export", label: "重试导出" };
  }
  if (job.status === "publish_failed" && capability === "auto_publish") {
    return { action: "retry", label: "重试发布" };
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
  audio: "音频",
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
