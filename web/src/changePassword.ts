export function validatePasswordChange(
  current: string,
  next: string,
  confirm: string,
): string | null {
  if (!current) return "请输入当前密码";
  if (next.length < 8) return "新密码至少 8 位";
  if (next.length > 128) return "新密码不能超过 128 位";
  if (next === current) return "新密码不能与当前密码相同";
  if (next !== confirm) return "两次输入的新密码不一致";
  return null;
}
