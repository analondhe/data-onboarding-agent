import type { ReactNode } from "react";

export function Notice({ tone, children }: { tone: "error" | "warn" | "ok"; children: ReactNode }) {
  const colors = {
    error: "bg-rose-50 text-rose-800",
    warn: "bg-amber-50 text-amber-900",
    ok: "bg-emerald-50 text-emerald-900",
  }[tone];
  return <p className={`rounded-md px-4 py-3 text-sm ${colors}`}>{children}</p>;
}
