import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export type Capability = "auto_publish" | "manual_export" | "pending";

export interface CredentialField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
}

export interface PlatformSpec {
  key: string;
  label: string;
  capability: Capability;
  content_capabilities: Record<string, Capability>;
  credential_fields: CredentialField[];
  meta_fields: string[];
  note: string;
}

export const FALLBACK_PLATFORMS: PlatformSpec[] = [
  {
    key: "wechat",
    label: "公众号",
    capability: "auto_publish",
    content_capabilities: { article: "auto_publish" },
    credential_fields: [
      {
        key: "app_id",
        label: "AppID",
        secret: false,
        required: true,
        placeholder: "公众号 AppID",
      },
      {
        key: "app_secret",
        label: "AppSecret",
        secret: true,
        required: true,
        placeholder: "公众号 AppSecret",
      },
    ],
    meta_fields: [
      "author",
      "thumb_media_id",
      "need_open_comment",
      "only_fans_can_comment",
    ],
    note: "使用公众号草稿接口，发布前需配置封面素材 ID",
  },
  {
    key: "douyin",
    label: "抖音",
    capability: "auto_publish",
    content_capabilities: { video: "auto_publish" },
    credential_fields: [
      {
        key: "client_key",
        label: "Client Key",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "client_secret",
        label: "Client Secret",
        secret: true,
        required: true,
        placeholder: "",
      },
      {
        key: "open_id",
        label: "Open ID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "refresh_token",
        label: "Refresh Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: [],
    note: "使用抖音开放平台视频发布接口",
  },
  {
    key: "toutiao",
    label: "头条",
    capability: "auto_publish",
    content_capabilities: {
      video: "auto_publish",
      article: "pending",
    },
    credential_fields: [
      {
        key: "client_key",
        label: "Client Key",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "client_secret",
        label: "Client Secret",
        secret: true,
        required: true,
        placeholder: "",
      },
      {
        key: "open_id",
        label: "Open ID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "refresh_token",
        label: "Refresh Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: ["author"],
    note: "视频走开放平台接口；图文接口已下线或未获批",
  },
  {
    key: "xiaohongshu",
    label: "小红书",
    capability: "manual_export",
    content_capabilities: {
      note: "manual_export",
      article: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "zhihu",
    label: "知乎",
    capability: "manual_export",
    content_capabilities: { article: "manual_export" },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "kuaishou",
    label: "快手",
    capability: "manual_export",
    content_capabilities: {
      video: "manual_export",
      note: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "channels",
    label: "视频号",
    capability: "manual_export",
    content_capabilities: {
      video: "manual_export",
      article: "manual_export",
    },
    credential_fields: [],
    meta_fields: [],
    note: "导出素材包后手动发布",
  },
  {
    key: "baijiahao",
    label: "百家号",
    capability: "pending",
    content_capabilities: { article: "pending" },
    credential_fields: [
      {
        key: "app_id",
        label: "AppID",
        secret: false,
        required: true,
        placeholder: "",
      },
      {
        key: "app_token",
        label: "App Token",
        secret: true,
        required: true,
        placeholder: "",
      },
    ],
    meta_fields: [],
    note: "官方发布接口尚未接入，可先保存凭据",
  },
  {
    key: "bilibili",
    label: "B站",
    capability: "pending",
    content_capabilities: { video: "pending" },
    credential_fields: [],
    meta_fields: [],
    note: "暂无官方发布接口，可先保存账号",
  },
];

export const CAPABILITY_LABELS: Record<Capability, string> = {
  auto_publish: "自动发布",
  manual_export: "人工导出",
  pending: "待接入",
};

export function capabilityFor(
  spec: PlatformSpec,
  contentKind: string,
): Capability {
  return spec.content_capabilities[contentKind] ?? "pending";
}

export function specFor(
  key: string,
  specs: PlatformSpec[] = FALLBACK_PLATFORMS,
): PlatformSpec | undefined {
  return specs.find((spec) => spec.key === key);
}

export function accountsForPlatform<
  T extends { platform: string; status: string },
>(accounts: T[], platform: string): T[] {
  return accounts.filter(
    (account) => account.platform === platform && account.status === "active",
  );
}

export function usePlatforms() {
  return useQuery({
    queryKey: ["platforms"],
    queryFn: () => api<PlatformSpec[]>("/accounts/platforms"),
    staleTime: 5 * 60 * 1000,
    placeholderData: FALLBACK_PLATFORMS,
  });
}
