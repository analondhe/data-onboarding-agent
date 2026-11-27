import type { Defect, FileInfo } from "@/lib/api";
import { DEFECT_LABEL, formatNumber } from "@/lib/format";

export function DefectList({ defects, files }: { defects: Defect[]; files: FileInfo[] }) {
  if (defects.length === 0) return <p className="text-slate-600">The agent found no data problems.</p>;
  const filename = Object.fromEntries(files.map((f) => [f.id, f.filename]));

  return (
    <div className="space-y-2">
      <p className="text-sm text-slate-600">
        Problems the agent found in the data. The mapping SQL is meant to fix them; rows it cannot fix are set
        aside with a reason in the report.
      </p>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
            <tr>
              <th className="px-4 py-2 font-medium">Problem</th>
              <th className="px-4 py-2 font-medium">Column</th>
              <th className="px-4 py-2 text-right font-medium">Rows</th>
              <th className="px-4 py-2 font-medium">What the agent saw</th>
              <th className="px-4 py-2 font-medium">Proposed fix</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 align-top">
            {defects.map((d) => (
              <tr key={d.id}>
                <td className="px-4 py-3 font-medium">{DEFECT_LABEL[d.defect_type] ?? d.defect_type}</td>
                <td className="px-4 py-3">
                  <span className="font-mono">{d.column_name ?? "–"}</span>
                  {d.target_column && d.target_column !== d.column_name && (
                    <span className="text-slate-500"> → {d.target_column}</span>
                  )}
                  {files.length > 1 && <span className="block text-xs text-slate-500">{filename[d.file_id]}</span>}
                </td>
                <td className="px-4 py-3 text-right tabular-nums">
                  {d.affected_rows === null ? "–" : formatNumber(d.affected_rows)}
                </td>
                <td className="px-4 py-3 text-slate-700">{d.description}</td>
                <td className="px-4 py-3 text-slate-700">{d.proposed_fix || "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
