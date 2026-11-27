import type { Role } from "./auth";

export const ROLE_LABELS: Record<Role, string> = {
  viewer: "只读",
  editor: "编辑",
  admin: "管理员",
  owner: "所有者",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role as Role] ?? role;
}

export function canManageMembers(role: Role): boolean {
  return role === "owner";
}

export function canEditMember(
  me: { id: string; role: Role },
  member: { id: string },
): boolean {
  return canManageMembers(me.role) && me.id !== member.id;
}

export function canPurgeMember(
  me: { id: string; role: Role },
  member: { id: string; is_active: boolean },
): boolean {
  return (
    canManageMembers(me.role) && me.id !== member.id && !member.is_active
  );
}

export function memberStatusLabel(isActive: boolean): string {
  return isActive ? "启用" : "已停用";
}
