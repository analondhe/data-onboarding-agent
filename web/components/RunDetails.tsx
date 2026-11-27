"use client";

import { type ReactNode, useEffect, useState } from "react";

import { BuildReport } from "@/components/BuildReport";
import { DefectList } from "@/components/DefectList";
import { MappingReview } from "@/components/MappingReview";
import { Notice } from "@/components/Notice";
import { api, type Defect, errorMessage, type Mapping, type Run, type SchemaColumn } from "@/lib/api";

interface Loaded {
  mappings: Mapping[];
  defects: Defect[];
  columns: Record<string, SchemaColumn>;
}

/** Mappings, defects, and (after a build) the report of a run that is not being worked on. */
export function RunDetails({
  run,
  mode,
  onBuild,
}: {
  run: Run;
  mode: "review" | "done" | "failed";
  onBuild: (run: Run) => void;
}) {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState(mode === "done" ? "report" : "mappings");

  useEffect(() => {
    Promise.all([api.mappings(run.id), api.defects(run.id), api.schemas()]).then(
      ([mappings, defects, schemas]) => {
        const target = schemas.find((s) => s.table === run.target_schema);
        setData({
          mappings,
          defects,
          columns: Object.fromEntries((target?.columns ?? []).map((c) => [c.name, c])),
        });
      },
      (err) => setError(errorMessage(err)),
    );
  }, [run.id, run.target_schema]);

  if (error) return <Notice tone="error">{error}</Notice>;
  if (data === null) return <p className="text-slate-500">Loading…</p>;

  const toReview = data.mappings.filter((m) => m.status === "proposed").length;
  const tabs: { id: string; label: string; content: ReactNode }[] = [
    {
      id: "mappings",
      label: toReview ? `Mappings (${toReview} to review)` : `Mappings (${data.mappings.length})`,
      content: (
        <MappingReview
          run={run}
          mappings={data.mappings}
          columns={data.columns}
          editable={mode !== "done"}
          onBuild={onBuild}
        />
      ),
    },
    {
      id: "defects",
      label: `Data problems (${data.defects.length})`,
      content: <DefectList defects={data.defects} files={run.files} />,
    },
  ];
  if (mode !== "review") tabs.unshift({ id: "report", label: "Report", content: <BuildReport runId={run.id} /> });

  return (
    <div>
      <div role="tablist" className="flex gap-1 border-b border-slate-200">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${
              tab === t.id ? "border-indigo-600 text-indigo-700" : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="pt-5">{tabs.find((t) => t.id === tab)?.content}</div>
    </div>
  );
}
