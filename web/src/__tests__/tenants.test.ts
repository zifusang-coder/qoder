import { describe, expect, it } from "vitest";
import {
  canEnableTenant,
  canSuspendTenant,
  tenantStatusLabel,
  validateTenantForm,
} from "../tenants";

const VALID_FORM = {
  name: "客户 A",
  slug: "customer-a",
  owner_email: "owner@example.com",
  owner_display_name: "客户管理员",
};

describe("validateTenantForm", () => {
  it("accepts a valid form", () => {
    expect(validateTenantForm(VALID_FORM)).toBeNull();
  });

  it("requires a name and display name", () => {
    expect(validateTenantForm({ ...VALID_FORM, name: "  " })).toBe(
      "请填写租户名称",
    );
    expect(validateTenantForm({ ...VALID_FORM, owner_display_name: " " })).toBe(
      "请填写所有者显示名",
    );
  });

  it("validates slug format", () => {
    expect(validateTenantForm({ ...VALID_FORM, slug: "A" })).toBe(
      "租户标识需为 2-50 位小写字母、数字或连字符",
    );
    expect(validateTenantForm({ ...VALID_FORM, slug: "customer_a" })).toBe(
      "租户标识需为 2-50 位小写字母、数字或连字符",
    );
  });

  it("validates the owner email", () => {
    expect(validateTenantForm({ ...VALID_FORM, owner_email: "owner" })).toBe(
      "请填写有效的所有者邮箱",
    );
    expect(
      validateTenantForm({ ...VALID_FORM, owner_email: "owner@example.com" }),
    ).toBeNull();
  });
});

describe("tenant status actions", () => {
  it("allows suspending only other active tenants", () => {
    expect(canSuspendTenant({ id: "t2", status: "active" }, "t1")).toBe(true);
    expect(canSuspendTenant({ id: "t1", status: "active" }, "t1")).toBe(false);
    expect(canSuspendTenant({ id: "t2", status: "suspended" }, "t1")).toBe(
      false,
    );
  });

  it("allows enabling only suspended tenants", () => {
    expect(canEnableTenant({ status: "suspended" })).toBe(true);
    expect(canEnableTenant({ status: "active" })).toBe(false);
  });

  it("labels tenant status", () => {
    expect(tenantStatusLabel("active")).toBe("启用");
    expect(tenantStatusLabel("suspended")).toBe("已停用");
    expect(tenantStatusLabel("other")).toBe("other");
  });
});
