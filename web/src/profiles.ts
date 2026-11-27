export function resolveProfileTarget<T extends { id: string }>(
  items: T[],
  selectedId: string,
): T | undefined {
  return items.find((item) => item.id === selectedId) ?? items[0];
}
