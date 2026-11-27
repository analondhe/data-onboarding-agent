"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusBadge } from "@/components/StatusBadge";
import { api, errorMessage, type Run } from "@/lib/api";
import { formatNumber, formatTime } from "@/lib/format";

export function RecentRuns() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.runs().then(setRuns, (err) => setError(errorMessage(err)));
  }, []);

  if (error) return <p className="text-sm text-rose-700">{error}</p>;
  if (runs === null) return <p className="text-sm text-slate-500">Loading…</p>;
  if (runs.length === 0) return <p className="text-sm text-slate-500">No runs yet.</p>;

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
          <tr>
            <th className="px-4 py-2 font-medium">Customer</th>
            <th className="px-4 py-2 font-medium">Table</th>
            <th className="px-4 py-2 font-medium">Files</th>
            <th className="px-4 py-2 font-medium">Status</th>
            <th className="px-4 py-2 font-medium">Started</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {runs.map((run) => (
            <tr key={run.id} className="hover:bg-slate-50">
              <td className="px-4 py-2">
                <Link href={`/runs/${run.id}`} className="font-medium text-indigo-700 hover:underline">
                  {run.customer_name}
                </Link>
              </td>
              <td className="px-4 py-2">{run.target_schema}</td>
              <td className="px-4 py-2 text-slate-600">
                {run.files.length === 0
                  ? "none"
                  : `${run.files.length} · ${formatNumber(run.files.reduce((n, f) => n + f.row_count, 0))} rows`}
              </td>
              <td className="px-4 py-2">
                <StatusBadge status={run.status} />
              </td>
              <td className="px-4 py-2 text-slate-600">{formatTime(run.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
