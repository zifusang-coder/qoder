export interface AuditFilters {
  action: string;
  status: string;
  actor_email: string;
  from: string;
  to: string;
}

export function buildAuditQuery(
  filters: AuditFilters,
  limit = 50,
  offset = 0,
): string {
  const params = new URLSearchParams();
  const action = filters.action.trim();
  const status = filters.status.trim();
  const actorEmail = filters.actor_email.trim();
  if (action) params.set("action", action);
  if (status) params.set("status", status);
  if (actorEmail) params.set("actor_email", actorEmail);
  if (filters.from) params.set("from", new Date(filters.from).toISOString());
  if (filters.to) params.set("to", new Date(filters.to).toISOString());
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  return params.toString();
}

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "auth.login": "登录成功",
  "auth.login_failed": "登录失败",
  "auth.change_password": "修改密码",
  "member.create": "创建成员",
  "member.update": "更新成员",
  "member.disable": "停用成员",
  "tenant.create": "创建租户",
  "tenant.update": "更新租户",
  "account.create": "创建发布账号",
  "account.update": "更新发布账号",
  "account.revoke": "停用发布账号",
  "provider.create": "创建模型提供商",
  "provider.delete": "删除模型提供商",
  "skill.toggle": "启停技能",
  "publish.create": "创建发布任务",
  "publish.execute": "执行发布",
  "publish.export": "导出素材包",
  "publish.schedule": "调整定时",
  "publish.action": "发布任务操作",
  "publish.auto_execute": "定时自动发布",
  "publish.auto_fail": "定时发布失败",
  "media.create": "创建媒体任务",
  "media.retry": "重试媒体任务",
};

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABELS[action] ?? action;
}

export const AUDIT_ACTION_OPTIONS = Object.keys(AUDIT_ACTION_LABELS);

export const AUDIT_STATUS_LABELS: Record<string, string> = {
  success: "成功",
  failure: "失败",
};

export function auditStatusLabel(status: string): string {
  return AUDIT_STATUS_LABELS[status] ?? status;
}
