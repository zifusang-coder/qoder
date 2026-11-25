export const TENANT_STATUS_LABELS: Record<string, string> = {
  active: "启用",
  suspended: "已停用",
};

export function tenantStatusLabel(status: string): string {
  return TENANT_STATUS_LABELS[status] ?? status;
}

export interface TenantForm {
  name: string;
  slug: string;
  owner_email: string;
  owner_display_name: string;
}

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,49}$/;
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+$/;

export function validateTenantForm(form: TenantForm): string | null {
  if (!form.name.trim()) return "请填写租户名称";
  if (!SLUG_PATTERN.test(form.slug.trim())) {
    return "租户标识需为 2-50 位小写字母、数字或连字符";
  }
  if (!EMAIL_PATTERN.test(form.owner_email.trim())) {
    return "请填写有效的所有者邮箱";
  }
  if (!form.owner_display_name.trim()) return "请填写所有者显示名";
  return null;
}

export function canSuspendTenant(
  tenant: { id: string; status: string },
  currentTenantId: string,
): boolean {
  return tenant.status === "active" && tenant.id !== currentTenantId;
}

export function canEnableTenant(tenant: { status: string }): boolean {
  return tenant.status === "suspended";
}

export function canDeleteTenant(
  tenant: { id: string; status: string },
  currentTenantId: string,
): boolean {
  return tenant.status === "suspended" && tenant.id !== currentTenantId;
}
