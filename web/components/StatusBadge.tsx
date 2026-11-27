import type { RunStatus } from "@/lib/api";
import { STATUS_LABEL } from "@/lib/format";

const COLORS: Record<RunStatus, string> = {
  created: "bg-slate-100 text-slate-700",
  profiling: "bg-sky-100 text-sky-800",
  mapping: "bg-sky-100 text-sky-800",
  awaiting_review: "bg-amber-100 text-amber-800",
  building: "bg-sky-100 text-sky-800",
  done: "bg-emerald-100 text-emerald-800",
  failed: "bg-rose-100 text-rose-800",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${COLORS[status]}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}
