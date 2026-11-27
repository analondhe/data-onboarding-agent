"use client";

import { type ReactNode, useEffect, useState } from "react";

import { Notice } from "@/components/Notice";
import { api, errorMessage, type NodeResult, type Report } from "@/lib/api";
import { formatDuration, formatNumber, formatPercent } from "@/lib/format";

const PASSED: Record<string, true> = { success: true, pass: true };

export function BuildReport({ runId }: { runId: string }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.report(runId).then(setReport, (err) => setError(errorMessage(err)));
  }, [runId]);

  if (error) return <Notice tone="error">{error}</Notice>;
  if (report === null) return <p className="text-slate-500">Loading…</p>;

  const build = report.build;
  const totals = (build?.row_accounting ?? []).reduce(
    (t, a) => ({ in: t.in + a.rows_in, loaded: t.loaded + a.loaded, rejected: t.rejected + a.rejected }),
    { in: 0, loaded: 0, rejected: 0 },
  );
  const share = (n: number) => (totals.in ? ` (${formatPercent(n / totals.in)})` : "");

  return (
    <div className="space-y-8">
      {build === null ? (
        <p className="text-slate-600">There is no build yet, so no rows have been loaded.</p>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="Rows in" value={formatNumber(totals.in)} />
            <Stat label="Loaded into the clean table" value={formatNumber(totals.loaded) + share(totals.loaded)} tone="ok" />
            <Stat
              label="Set aside (rejected)"
              value={formatNumber(totals.rejected) + share(totals.rejected)}
              tone={totals.rejected ? "warn" : undefined}
            />
          </div>

          {build.load && (
            <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
              Loaded into <span className="font-mono">{build.load.table}</span>:{" "}
              <strong>{formatNumber(build.load.inserted)}</strong> new{" "}
              {build.load.inserted === 1 ? "row" : "rows"}, <strong>{formatNumber(build.load.updated)}</strong>{" "}
              updated, <strong>{formatNumber(build.load.total_rows)}</strong> rows in the table now.
            </p>
          )}

          <Notice tone={build.status === "passed" ? "ok" : "error"}>
            Build {build.status === "passed" ? "passed" : "failed"}
            {build.repairs > 0 && ` after ${build.repairs} ${build.repairs === 1 ? "repair" : "repairs"} by the agent`}.
            {" "}dbt models: {build.dbt.models_passed} passed, {build.dbt.models_failed} failed. Data tests:{" "}
            {build.dbt.tests_passed} passed, {build.dbt.tests_failed} failed.
            {build.dbt.error && ` ${build.dbt.error}`}
          </Notice>

          <section>
            <h3 className="font-semibold">Rows per file</h3>
            <p className="text-sm text-slate-600">
              Every input row is either loaded or set aside with a reason; none are dropped.
            </p>
            <div className="mt-3 space-y-3">
              {build.row_accounting.map((a) => (
                <div key={a.file_id} className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-mono">{a.filename ?? a.file_id}</span>
                    <span className="text-sm">
                      {formatNumber(a.rows_in)} in = {formatNumber(a.loaded)} loaded + {formatNumber(a.rejected)}{" "}
                      set aside{" "}
                      {a.balanced ? (
                        <span className="text-emerald-700">✓ adds up</span>
                      ) : (
                        <span className="font-medium text-rose-700">✗ does not add up</span>
                      )}
                    </span>
                  </div>
                  {Object.keys(a.reject_reasons).length > 0 && (
                    <table className="mt-3 w-full text-left text-sm">
                      <thead className="text-slate-500">
                        <tr>
                          <th className="py-1 font-medium">Why rows were set aside</th>
                          <th className="py-1 text-right font-medium">Rows</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {Object.entries(a.reject_reasons).map(([reason, n]) => (
                          <tr key={reason}>
                            <td className="py-1.5 font-mono text-xs">{reason}</td>
                            <td className="py-1.5 text-right tabular-nums">{formatNumber(n)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3 className="font-semibold">dbt checks</h3>
            <DbtResults results={build.dbt.results} />
          </section>
        </>
      )}

      <section>
        <h3 className="font-semibold">How the run went</h3>
        <dl className="mt-3 grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
          <Detail term="Model">{report.run.model_name ?? "–"}</Detail>
          <Detail term="Run time">{formatDuration(report.duration_seconds)}</Detail>
          <Detail term="Model calls">{formatNumber(report.model_calls)}</Detail>
          <Detail term="Tool calls">{formatNumber(report.tool_calls)}</Detail>
          <Detail term="Tokens">
            {formatNumber(report.prompt_tokens + report.completion_tokens)} ({formatNumber(report.prompt_tokens)} in,{" "}
            {formatNumber(report.completion_tokens)} out)
          </Detail>
          <Detail term="Trace">
            {report.trace_url ? (
              <a href={report.trace_url} target="_blank" rel="noreferrer" className="text-indigo-700 underline">
                Open in MLflow ↗
              </a>
            ) : (
              "not recorded"
            )}
          </Detail>
        </dl>
      </section>
    </div>
  );
}

function DbtResults({ results }: { results: NodeResult[] }) {
  const failing = results.filter((r) => !PASSED[r.status]);
  const rows = [...failing, ...results.filter((r) => PASSED[r.status])];
  return (
    <details open={failing.length > 0} className="mt-2 rounded-lg border border-slate-200 bg-white shadow-sm">
      <summary className="cursor-pointer px-4 py-2 text-sm text-slate-700">
        {results.length} models and tests{failing.length > 0 && `, ${failing.length} not passing`}
      </summary>
      <table className="w-full text-left text-sm">
        <thead className="border-y border-slate-200 bg-slate-50 text-slate-600">
          <tr>
            <th className="px-4 py-2 font-medium">Name</th>
            <th className="px-4 py-2 font-medium">Kind</th>
            <th className="px-4 py-2 font-medium">Result</th>
            <th className="px-4 py-2 font-medium">Details</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 align-top">
          {rows.map((r) => (
            <tr key={r.name}>
              <td className="px-4 py-2 font-mono text-xs">{r.name}</td>
              <td className="px-4 py-2">{r.kind}</td>
              <td className={`px-4 py-2 ${PASSED[r.status] ? "text-emerald-700" : "font-medium text-rose-700"}`}>
                {r.status}
              </td>
              <td className="px-4 py-2 text-xs text-slate-600">
                {r.failures ? `${formatNumber(r.failures)} failing rows. ` : ""}
                {PASSED[r.status] ? "" : r.message}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" }) {
  const color = tone === "ok" ? "text-emerald-700" : tone === "warn" ? "text-amber-700" : "text-slate-900";
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-sm text-slate-600">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</p>
    </div>
  );
}

function Detail({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5">
      <dt className="text-slate-600">{term}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}
