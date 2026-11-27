"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { FileDrop } from "@/components/FileDrop";
import { Notice } from "@/components/Notice";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ContractOut, errorMessage, type Run, uploadLoad } from "@/lib/api";
import { formatBytes, formatNumber, formatPercent, formatTime } from "@/lib/format";

export function SourceView({ id }: { id: string }) {
  const router = useRouter();
  const [contract, setContract] = useState<ContractOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  useEffect(() => {
    api.contract(id).then(setContract, (err) => setLoadError(errorMessage(err)));
  }, [id]);

  async function load(files: File[]) {
    const next = files[0];
    if (!next) return;
    setFile(next);
    setProgress(0);
    setUploadError(null);
    setUploading(true);
    try {
      const run = await uploadLoad(id, next, setProgress);
      router.push(`/runs/${run.id}`);
    } catch (err) {
      setUploadError(errorMessage(err));
      setUploading(false);
    }
  }

  if (contract === null) {
    return loadError ? <Notice tone="error">{loadError}</Notice> : <p className="text-slate-500">Loading…</p>;
  }

  const latest = contract.versions[0];

  return (
    <div className="space-y-8">
      <div>
        <Link href="/sources" className="text-sm text-slate-600 hover:underline">
          ← All sources
        </Link>
        <h1 className="mt-2 text-2xl font-semibold">
          {contract.customer_name} · {contract.source_name}
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          Table <span className="font-mono">{contract.target_schema}</span> · version {contract.version}, saved{" "}
          {formatTime(contract.created_at)} from{" "}
          <Link href={`/runs/${contract.created_from_run_id}`} className="text-indigo-700 underline">
            this run
          </Link>{" "}
          · {contract.table_rows === null ? "no rows loaded yet" : `${formatNumber(contract.table_rows)} rows in the table`}
        </p>
      </div>

      {latest && latest.id !== contract.id && (
        <Notice tone="warn">
          You are looking at version {contract.version}. The newest is version {latest.version};{" "}
          <Link href={`/sources/${latest.id}`} className="font-medium underline">
            open it
          </Link>
          . New files are always checked against the newest version.
        </Notice>
      )}

      <section>
        <h2 className="text-xl font-semibold">Load a new file</h2>
        <p className="mt-1 text-sm text-slate-600">
          Drop the next file from this source. It is compared with the newest saved version first: if nothing
          changed it is loaded with the saved mappings, without asking you; if something changed, the load waits
          for your review.
        </p>
        <div className="mt-4">
          <FileDrop onFiles={load} disabled={uploading} multiple={false} />
        </div>
        {file && (
          <div className="mt-3 rounded-md border border-slate-200 bg-white px-4 py-3 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span className="font-mono">{file.name}</span>
              <span className="text-slate-500">
                {formatBytes(file.size)}{" "}
                {uploading ? (
                  <span className="ml-2 text-indigo-700">
                    {progress < 1 ? `Uploading ${formatPercent(progress)}` : "Starting the load…"}
                  </span>
                ) : (
                  uploadError && <span className="ml-2 text-rose-700">Rejected</span>
                )}
              </span>
            </div>
            {uploading && (
              <div className="mt-2 h-1.5 overflow-hidden rounded bg-slate-100">
                <div className="h-full bg-indigo-500" style={{ width: formatPercent(progress) }} />
              </div>
            )}
          </div>
        )}
        {uploadError && (
          <div className="mt-3">
            <Notice tone="error">{uploadError}</Notice>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-xl font-semibold">Recent loads</h2>
        <RecentLoads runs={contract.recent_loads} />
      </section>

      <section>
        <h2 className="text-xl font-semibold">Saved mappings</h2>
        <p className="mt-1 text-sm text-slate-600">
          How each column of the file becomes a column of the table. The file this version was saved from had{" "}
          {formatNumber(contract.row_count)} rows and {contract.columns.length} columns.
        </p>
        <Table head={["File column", "Table column", "Transform SQL"]}>
          {contract.mappings.map((m) => (
            <tr key={`${m.source_column}:${m.target_column}`} className="align-top">
              <td className="px-4 py-2 font-mono">{m.source_column}</td>
              <td className="px-4 py-2 font-mono font-semibold">{m.target_column}</td>
              <td className="px-4 py-2">
                <code className="whitespace-pre-wrap break-all text-xs text-slate-700">{m.transform_sql ?? "–"}</code>
              </td>
            </tr>
          ))}
        </Table>
      </section>

      <section>
        <h2 className="text-xl font-semibold">Version history</h2>
        <p className="mt-1 text-sm text-slate-600">
          A new version is saved when a load changes the mappings or the file&apos;s columns.
        </p>
        <Table head={["Version", "Saved", "Saved from"]}>
          {contract.versions.map((v) => (
            <tr key={v.id} className={v.id === contract.id ? "bg-indigo-50/50" : undefined}>
              <td className="px-4 py-2">
                {v.id === contract.id ? (
                  <span className="font-medium">Version {v.version} (shown)</span>
                ) : (
                  <Link href={`/sources/${v.id}`} className="text-indigo-700 hover:underline">
                    Version {v.version}
                  </Link>
                )}
              </td>
              <td className="px-4 py-2 text-slate-600">{formatTime(v.created_at)}</td>
              <td className="px-4 py-2">
                <Link href={`/runs/${v.created_from_run_id}`} className="text-indigo-700 hover:underline">
                  Run
                </Link>
              </td>
            </tr>
          ))}
        </Table>
      </section>
    </div>
  );
}

function RecentLoads({ runs }: { runs: Run[] }) {
  if (runs.length === 0) {
    return <p className="mt-3 text-sm text-slate-500">No files have been loaded into this source yet.</p>;
  }
  return (
    <Table head={["Status", "File", "Rows", "Changes found", "Started", ""]}>
      {runs.map((run) => (
        <tr key={run.id} className="hover:bg-slate-50">
          <td className="px-4 py-2">
            <StatusBadge status={run.status} />
          </td>
          <td className="px-4 py-2 font-mono">{run.files[0]?.filename ?? "–"}</td>
          <td className="px-4 py-2 tabular-nums">{formatNumber(run.files.reduce((n, f) => n + f.row_count, 0))}</td>
          <td className="px-4 py-2 text-slate-600">
            {run.drift === null ? "–" : run.drift.length === 0 ? "none" : formatNumber(run.drift.length)}
          </td>
          <td className="px-4 py-2 text-slate-600">{formatTime(run.created_at)}</td>
          <td className="px-4 py-2 text-right">
            <Link href={`/runs/${run.id}`} className="text-indigo-700 hover:underline">
              Open
            </Link>
          </td>
        </tr>
      ))}
    </Table>
  );
}

function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-4 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  );
}
