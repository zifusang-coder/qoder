import type { ReactNode } from "react";
import type { NavIconKey } from "../auth";

const ICONS: Record<NavIconKey, ReactNode> = {
  assistant: (
    <>
      <path d="M11 4l1.6 3.8L16.4 9.4l-3.8 1.6L11 14.8 9.4 11 5.6 9.4 9.4 7.8 11 4z" />
      <path d="M17.5 14l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8.8-1.9z" />
    </>
  ),
  workflow: (
    <>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="6" cy="18" r="2.2" />
      <circle cx="18" cy="12" r="2.2" />
      <path d="M6 8.2v7.6M18 12h-6a4 4 0 0 1-4-4" />
    </>
  ),
  chat: <path d="M4 5h16v11H8l-4 4V5z" />,
  profiles: (
    <>
      <circle cx="12" cy="8" r="3.4" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>
  ),
  projects: (
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
  ),
  accounts: (
    <>
      <rect x="4" y="5" width="16" height="14" rx="2" />
      <circle cx="9" cy="11" r="1.8" />
      <path d="M13 10h4M13 14h4" />
    </>
  ),
  calendar: (
    <>
      <rect x="4" y="5" width="16" height="15" rx="2" />
      <path d="M4 9.5h16M9 3v4M15 3v4" />
    </>
  ),
  publish: <path d="M4 12l16-8-6 16-2.5-6.5L4 12z" />,
  analytics: <path d="M5 20V10M12 20V4M19 20v-7" />,
  settings: (
    <>
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="9" cy="17" r="2" />
    </>
  ),
  team: (
    <>
      <circle cx="9.5" cy="8" r="3" />
      <path d="M3.5 20a6 6 0 0 1 12 0" />
      <path d="M16 5.5a3 3 0 0 1 0 5" />
      <path d="M17.5 14.2A5 5 0 0 1 21 19" />
    </>
  ),
  audit: (
    <>
      <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z" />
      <path d="M9 12l2 2 4-4" />
    </>
  ),
  tenants: (
    <>
      <path d="M4 21V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v16" />
      <path d="M15 9h3a1 1 0 0 1 1 1v11" />
      <path d="M8 8h3M8 12h3M8 16h3" />
    </>
  ),
};

export default function NavIcon({
  name,
  className,
}: {
  name: NavIconKey;
  className?: string;
}) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {ICONS[name]}
    </svg>
  );
}
