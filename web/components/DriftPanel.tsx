import type { DriftAction, Finding } from "@/lib/api";
import { DRIFT_CHECK_LABEL } from "@/lib/format";

const ACTION: Record<DriftAction, { label: string; icon: string; row: string; badge: string }> = {
  block: { label: "Load blocked", icon: "✕", row: "border-rose-200 bg-rose-50", badge: "bg-rose-600 text-white" },
  propose: {
    label: "Needs your review",
    icon: "?",
    row: "border-amber-200 bg-amber-50",
    badge: "bg-amber-500 text-white",
  },
  warn: {
    label: "Warning, load continues",
    icon: "!",
    row: "border-slate-200 bg-white",
    badge: "bg-amber-100 text-amber-800",
  },
};
const ORDER: DriftAction[] = ["block", "propose", "warn"];

/** What the drift checks found when comparing a recurring load's file with the saved source. */
export function DriftPanel({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return (
      <p className="rounded-md bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
        ✓ No changes from the saved source; loaded with the saved mappings.
      </p>
    );
  }

  const sorted = [...findings].sort((a, b) => ORDER.indexOf(a.action) - ORDER.indexOf(b.action));
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold">What changed</h2>
      <p className="text-sm text-slate-600">
        Differences between this file and the saved source, found before anything was loaded.
      </p>
      <ul className="mt-3 space-y-2">
        {sorted.map((f, index) => {
          const action = ACTION[f.action];
          return (
            <li key={index} className={`flex gap-3 rounded-md border px-4 py-3 ${action.row}`}>
              <span
                aria-hidden
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-sm font-bold ${action.badge}`}
              >
                {action.icon}
              </span>
              <div className="min-w-0 text-sm">
                <p>
                  <span className="font-medium">{action.label}</span>
                  <span className="text-slate-500"> · {DRIFT_CHECK_LABEL[f.check] ?? f.check}</span>
                </p>
                <p className="mt-0.5 text-slate-800">{f.message}</p>
                <Columns finding={f} />
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Columns({ finding: f }: { finding: Finding }) {
  if (!f.source_column && !f.target_column) return null;
  return (
    <p className="mt-1 text-xs text-slate-600">
      Column{" "}
      {f.source_column && <span className="font-mono">{f.source_column}</span>}
      {f.source_column && f.target_column && f.target_column !== f.source_column && (
        <span className="text-slate-400"> → </span>
      )}
      {f.target_column && f.target_column !== f.source_column && (
        <span className="font-mono">{f.target_column}</span>
      )}
    </p>
  );
}
