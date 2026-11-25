export type Role = "viewer" | "editor" | "admin" | "owner";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  tenant_id: string;
  tenant_name: string;
  is_platform_admin: boolean;
  must_change_password: boolean;
}

export const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  editor: 1,
  admin: 2,
  owner: 3,
};

export function atLeast(role: Role, minRole: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[minRole];
}

export function canWrite(role: Role): boolean {
  return atLeast(role, "editor");
}

export function isAdmin(role: Role): boolean {
  return atLeast(role, "admin");
}

export function isOwner(role: Role): boolean {
  return role === "owner";
}

export type NavGroupKey = "ai" | "content" | "manage";

export const NAV_GROUP_LABELS: Record<NavGroupKey, string> = {
  ai: "AI",
  content: "内容",
  manage: "管理",
};

export type NavIconKey =
  | "assistant"
  | "workflow"
  | "chat"
  | "profiles"
  | "projects"
  | "accounts"
  | "calendar"
  | "publish"
  | "analytics"
  | "settings"
  | "team"
  | "audit"
  | "tenants";

export interface NavItem {
  to: string;
  label: string;
  minRole: Role;
  platformOnly?: boolean;
  group: NavGroupKey;
  icon: NavIconKey;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/assistant", label: "AI 助理", minRole: "editor", group: "ai", icon: "assistant" },
  { to: "/workflows", label: "工作流", minRole: "editor", group: "ai", icon: "workflow" },
  { to: "/chat", label: "助手", minRole: "editor", group: "ai", icon: "chat" },
  { to: "/profiles", label: "账号画像", minRole: "viewer", group: "content", icon: "profiles" },
  { to: "/projects", label: "项目", minRole: "viewer", group: "content", icon: "projects" },
  { to: "/accounts", label: "发布账号", minRole: "editor", group: "content", icon: "accounts" },
  { to: "/calendar", label: "日历", minRole: "viewer", group: "content", icon: "calendar" },
  { to: "/publish", label: "发布", minRole: "viewer", group: "content", icon: "publish" },
  { to: "/analytics", label: "数据", minRole: "viewer", group: "content", icon: "analytics" },
  { to: "/settings", label: "设置", minRole: "admin", group: "manage", icon: "settings" },
  { to: "/team", label: "团队", minRole: "admin", group: "manage", icon: "team" },
  { to: "/audit", label: "审计", minRole: "admin", group: "manage", icon: "audit" },
  { to: "/tenants", label: "租户", minRole: "viewer", platformOnly: true, group: "manage", icon: "tenants" },
];

export function visibleNavItems(
  me: Pick<Me, "role" | "is_platform_admin">,
): NavItem[] {
  return NAV_ITEMS.filter((item) =>
    item.platformOnly
      ? me.is_platform_admin
      : atLeast(me.role, item.minRole),
  );
}

export function mustChangePath(
  me: Pick<Me, "must_change_password">,
  pathname: string,
): string | null {
  if (me.must_change_password && pathname !== "/change-password") {
    return "/change-password";
  }
  return null;
}

export function defaultPath(me: Pick<Me, "role">): string {
  return canWrite(me.role) ? "/chat" : "/projects";
}
