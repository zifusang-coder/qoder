import { describe, expect, it } from "vitest";
import {
  EMPTY_FEED,
  notificationAge,
  readSeenAt,
  severityDot,
  sourceLabel,
  unseenCount,
  writeSeenAt,
  type AppNotification,
} from "../notifications";

function note(overrides: Partial<AppNotification>): AppNotification {
  return {
    id: "approval:run-1",
    kind: "approval",
    source: "assistant",
    severity: "warn",
    title: "AI 助理等待审批",
    detail: "publish.approve",
    href: "/assistant",
    created_at: "2026-11-23T10:00:00+00:00",
    ...overrides,
  };
}

describe("unseenCount", () => {
  const items = [
    note({ id: "a", created_at: "2026-11-23T10:00:00+00:00" }),
    note({ id: "b", created_at: "2026-11-23T11:00:00+00:00" }),
    note({ id: "c", created_at: "2026-11-23T12:00:00+00:00" }),
  ];

  it("counts everything when nothing was read yet", () => {
    expect(unseenCount(items, null)).toBe(3);
  });

  it("only counts items newer than the seen timestamp", () => {
    expect(unseenCount(items, "2026-11-23T10:30:00+00:00")).toBe(2);
    expect(unseenCount(items, "2026-11-23T12:00:00+00:00")).toBe(0);
  });

  it("treats missing timestamps as unseen", () => {
    expect(unseenCount([note({ created_at: null })], "2026-11-23T12:00:00+00:00")).toBe(1);
  });

  it("ignores unparsable timestamps", () => {
    expect(unseenCount([note({ created_at: "yesterday" })], null)).toBe(0);
  });
});

describe("notificationAge", () => {
  const now = new Date("2026-11-23T12:00:00+00:00");

  it("formats recent notifications", () => {
    expect(notificationAge("2026-11-23T11:59:30+00:00", now)).toBe("刚刚");
    expect(notificationAge("2026-11-23T11:55:00+00:00", now)).toBe("5 分钟前");
    expect(notificationAge("2026-11-23T09:00:00+00:00", now)).toBe("3 小时前");
    expect(notificationAge("2026-11-21T12:00:00+00:00", now)).toBe("2 天前");
  });

  it("falls back to a short date for older items", () => {
    expect(notificationAge("2026-11-01T12:00:00+00:00", now)).toBe("11 月 1 日");
  });

  it("returns an empty string without a timestamp", () => {
    expect(notificationAge(null, now)).toBe("");
  });
});

describe("labels and tones", () => {
  it("maps sources to Chinese labels with a fallback", () => {
    expect(sourceLabel("assistant")).toBe("AI 助理");
    expect(sourceLabel("workflow")).toBe("工作流");
    expect(sourceLabel("publish")).toBe("发布");
    expect(sourceLabel("media")).toBe("媒体");
    expect(sourceLabel("mystery")).toBe("mystery");
  });

  it("maps severity to pastel status dots", () => {
    expect(severityDot("warn")).toBe("bg-honey-600");
    expect(severityDot("error")).toBe("bg-coral-500");
  });

  it("ships an empty feed with zeroed counts", () => {
    expect(EMPTY_FEED.items).toEqual([]);
    expect(EMPTY_FEED.counts).toEqual({ approval: 0, review: 0, failure: 0 });
  });
});

describe("seen timestamp storage", () => {
  function fakeStorage(initial: string | null) {
    let value = initial;
    return {
      getItem: () => value,
      setItem: (_key: string, next: string) => {
        value = next;
      },
      value: () => value,
    };
  }

  it("reads and writes the seen timestamp", () => {
    const storage = fakeStorage(null);
    expect(readSeenAt(storage)).toBeNull();
    writeSeenAt(storage, "2026-11-23T12:00:00+00:00");
    expect(storage.value()).toBe("2026-11-23T12:00:00+00:00");
    expect(readSeenAt(storage)).toBe("2026-11-23T12:00:00+00:00");
  });

  it("tolerates storage failures", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };
    expect(readSeenAt(broken)).toBeNull();
    expect(() => writeSeenAt(broken, "2026-11-23T12:00:00+00:00")).not.toThrow();
  });
});
