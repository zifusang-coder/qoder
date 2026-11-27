import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../client";
import { useLiveSnapshot } from "../live";
import {
  EMPTY_FEED,
  notificationAge,
  readSeenAt,
  severityDot,
  sourceLabel,
  unseenCount,
  writeSeenAt,
  type NotificationFeed,
} from "../notifications";

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [seenAt, setSeenAt] = useState<string | null>(() =>
    readSeenAt(window.localStorage),
  );

  const feed = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api<NotificationFeed>("/notifications"),
    refetchInterval: 60_000,
  });
  useLiveSnapshot<NotificationFeed>(["notifications"], "/notifications/stream");

  const data = feed.data ?? EMPTY_FEED;
  const unseen = unseenCount(data.items, seenAt);
  const now = new Date();

  function markAllRead() {
    const at = new Date().toISOString();
    writeSeenAt(window.localStorage, at);
    setSeenAt(at);
  }

  return (
    <div className="relative">
      <button
        type="button"
        className="nav-link relative"
        aria-label="通知"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        通知
        {unseen > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-coral-500 px-1 text-[10px] font-semibold leading-none text-white">
            {unseen > 99 ? "99+" : unseen}
          </span>
        )}
      </button>
      {open && (
        <>
          <div
            className="fixed inset-0 z-30"
            aria-hidden="true"
            onClick={() => setOpen(false)}
          />
          <div
            role="menu"
            className="absolute right-0 top-full z-40 mt-2 w-80 overflow-hidden rounded-xl border border-line bg-white shadow-lg"
          >
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="text-sm font-medium text-ink-700">通知</span>
              <button
                type="button"
                className="text-xs text-brand-700 hover:underline disabled:text-ink-500 disabled:no-underline"
                disabled={unseen === 0}
                onClick={markAllRead}
              >
                全部标为已读
              </button>
            </div>
            {data.items.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-ink-500">
                暂无需要处理的事项
              </p>
            ) : (
              <ul className="max-h-96 overflow-y-auto">
                {data.items.map((item) => (
                  <li key={item.id} className="border-b border-line/60 last:border-b-0">
                    <Link
                      to={item.href}
                      role="menuitem"
                      className="flex gap-2 px-4 py-2.5 hover:bg-brand-50/60"
                      onClick={() => setOpen(false)}
                    >
                      <span
                        aria-hidden="true"
                        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${severityDot(item.severity)}`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-baseline justify-between gap-2">
                          <span className="truncate text-sm font-medium text-ink-900">
                            {item.title}
                          </span>
                          <span className="shrink-0 text-[11px] text-ink-500">
                            {notificationAge(item.created_at, now)}
                          </span>
                        </span>
                        <span className="mt-0.5 block truncate text-xs text-ink-500">
                          {sourceLabel(item.source)} · {item.detail}
                        </span>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
