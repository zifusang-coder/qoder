export const DEFAULT_SLOTS = ["09:00", "12:00", "18:00", "21:00"];

/** 距当前时间过近的时段跳过，留出保存与审核时间。 */
export const SLOT_LEAD_MINUTES = 30;

const DAY_MS = 86_400_000;

export interface SlotSuggestion {
  slot: string;
  scheduledAt: string;
  label: string;
  dayLabel: string;
  recommended: boolean;
}

function parseSlot(slot: string): { hours: number; minutes: number } | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(slot.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return null;
  return { hours, minutes };
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function slotToDate(day: Date, slot: string): Date | null {
  const parsed = parseSlot(slot);
  if (!parsed) return null;
  return new Date(
    day.getFullYear(),
    day.getMonth(),
    day.getDate(),
    parsed.hours,
    parsed.minutes,
    0,
    0,
  );
}

export function dayLabel(day: Date, today: Date): string {
  const diffDays = Math.round((startOfDay(day) - startOfDay(today)) / DAY_MS);
  if (diffDays === 0) return "今天";
  if (diffDays === 1) return "明天";
  if (diffDays === 2) return "后天";
  return `${day.getMonth() + 1} 月 ${day.getDate()} 日`;
}

export function slotSuggestions(
  now: Date,
  slots: string[] = DEFAULT_SLOTS,
  options: { days?: number; leadMinutes?: number } = {},
): SlotSuggestion[] {
  const days = options.days ?? 3;
  const cutoff = now.getTime() + (options.leadMinutes ?? SLOT_LEAD_MINUTES) * 60_000;
  const results: SlotSuggestion[] = [];
  for (let offset = 0; offset < days; offset += 1) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
    const label = dayLabel(day, now);
    for (const slot of slots) {
      const date = slotToDate(day, slot);
      if (!date || date.getTime() < cutoff) continue;
      results.push({
        slot,
        scheduledAt: date.toISOString(),
        label: `${label} ${slot}`,
        dayLabel: label,
        recommended: false,
      });
    }
  }
  results.sort((left, right) => left.scheduledAt.localeCompare(right.scheduledAt));
  if (results.length > 0) results[0] = { ...results[0], recommended: true };
  return results;
}
