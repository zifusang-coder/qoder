import type { ReactNode } from "react";

export default function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-white/60 px-6 py-8 text-center">
      <p className="text-sm font-medium text-ink-700">{title}</p>
      {hint && <p className="mt-1 text-sm text-ink-500">{hint}</p>}
      {action && <div className="mt-3 flex justify-center">{action}</div>}
    </div>
  );
}

export function InlineError({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-coral-100 bg-coral-50 px-3 py-2 text-sm text-coral-600">
      {children}
    </p>
  );
}
