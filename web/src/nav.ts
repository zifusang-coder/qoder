import {
  NAV_GROUP_LABELS,
  type NavGroupKey,
  type NavItem,
} from "./auth";

export { NAV_GROUP_LABELS } from "./auth";

export interface NavGroup {
  key: NavGroupKey;
  label: string;
  items: NavItem[];
}

const GROUP_ORDER: NavGroupKey[] = ["ai", "content", "manage"];

export function groupedNav(items: NavItem[]): NavGroup[] {
  return GROUP_ORDER.map((key) => ({
    key,
    label: NAV_GROUP_LABELS[key],
    items: items.filter((item) => item.group === key),
  })).filter((group) => group.items.length > 0);
}
