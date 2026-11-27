"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusBadge } from "@/components/StatusBadge";
import { api, type ContractSummary, errorMessage } from "@/lib/api";
import { formatNumber, formatTime } from "@/lib/format";

export function SourceList() {
  const [sources, setSources] = useState<ContractSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.contracts().then(setSources, (err) => setError(errorMessage(err)));
  }, []);

  if (error) return <p className="text-sm text-rose-700">{error}</p>;
  if (sources === null) return <p className="text-sm text-slate-500">Loading…</p>;
  if (sources.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-sm text-slate-600">
        <p className="font-medium text-slate-800">No saved sources yet.</p>
        <p className="mt-1">
          A source is saved from a finished run: open a run that is <strong>Done</strong> and use{" "}
          <strong>Save as recurring source</strong>. After that, new files of the same kind can be loaded here
          without starting a new run from scratch.
        </p>
        <Link href="/" className="mt-3 inline-block text-indigo-700 underline">
          Start a new run
        </Link>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
          <tr>
            <th className="px-4 py-2 font-medium">Customer</th>
            <th className="px-4 py-2 font-medium">Source</th>
            <th className="px-4 py-2 font-medium">Table</th>
            <th className="px-4 py-2 text-right font-medium">Version</th>
            <th className="px-4 py-2 text-right font-medium">Loads</th>
            <th className="px-4 py-2 font-medium">Last load</th>
            <th className="px-4 py-2 text-right font-medium">Rows in table</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sources.map((s) => (
            <tr key={s.id} className="hover:bg-slate-50">
              <td className="px-4 py-2">{s.customer_name}</td>
              <td className="px-4 py-2">
                <Link href={`/sources/${s.id}`} className="font-medium text-indigo-700 hover:underline">
                  {s.source_name}
                </Link>
              </td>
              <td className="px-4 py-2 font-mono">{s.target_schema}</td>
              <td className="px-4 py-2 text-right tabular-nums">{s.version}</td>
              <td className="px-4 py-2 text-right tabular-nums">{formatNumber(s.loads)}</td>
              <td className="px-4 py-2">
                {s.last_load ? (
                  <Link href={`/runs/${s.last_load.run_id}`} className="flex items-center gap-2 hover:underline">
                    <StatusBadge status={s.last_load.status} />
                    <span className="text-slate-600">{formatTime(s.last_load.created_at)}</span>
                  </Link>
                ) : (
                  <span className="text-slate-500">no loads yet</span>
                )}
              </td>
              <td className="px-4 py-2 text-right tabular-nums">
                {s.table_rows === null ? "–" : formatNumber(s.table_rows)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
