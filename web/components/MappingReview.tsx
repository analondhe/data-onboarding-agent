"use client";

import { useState } from "react";

import { Notice } from "@/components/Notice";
import { type Action, api, type Decision, errorMessage, type Mapping, type Run, type SchemaColumn } from "@/lib/api";
import { formatNumber, formatPercent } from "@/lib/format";

interface Draft {
  action: Action;
  sql?: string; // for "edit"
}

const ACTION_LABEL: Record<Action, string> = { approve: "Approve", edit: "Edit", reject: "Reject" };

/** One card per proposed mapping, least confident first, with approve / edit / reject. */
export function MappingReview({
  run,
  mappings,
  columns,
  editable,
  onBuild,
}: {
  run: Run;
  mappings: Mapping[];
  columns: Record<string, SchemaColumn>;
  editable: boolean;
  onBuild: (run: Run) => void;
}) {
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (mappings.length === 0) return <p className="text-slate-600">The agent has not proposed any mappings yet.</p>;

  const ordered = [...mappings].sort((a, b) => (a.confidence ?? -1) - (b.confidence ?? -1) || a.id - b.id);
  const filename = Object.fromEntries(run.files.map((f) => [f.id, f.filename]));
  const undecided = mappings.filter((m) => m.status === "proposed" && !drafts[m.id]).length;
  const changes = Object.keys(drafts).length;

  const decide = (id: number, draft: Draft | null) =>
    setDrafts((prev) => {
      const next = { ...prev };
      if (draft) next[id] = draft;
      else delete next[id];
      return next;
    });

  async function submit() {
    setError(null);
    setSubmitting(true);
    const decisions: Decision[] = Object.entries(drafts).map(([id, d]) => ({
      mapping_id: Number(id),
      action: d.action,
      ...(d.action === "edit" ? { transform_sql: d.sql } : {}),
    }));
    try {
      const result = await api.review(run.id, decisions);
      if (result.building) onBuild(result.run);
      else setError(`${result.pending_reviews} mappings still need a decision.`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      {editable && (
        <p className="text-sm text-slate-600">
          Each card is one column of the uploaded file and where the agent wants to put it. Cards marked{" "}
          <span className="font-medium text-amber-800">needs your decision</span> are ones the agent is unsure about;
          the rest were approved automatically but you can still change them.
        </p>
      )}
      <ul className="space-y-3">
        {ordered.map((m) => (
          <MappingCard
            key={m.id}
            mapping={m}
            column={columns[m.target_column]}
            filename={run.files.length > 1 ? filename[m.file_id] : undefined}
            draft={drafts[m.id]}
            editable={editable && !submitting}
            editing={editing?.id === m.id ? editing.text : null}
            onDecide={(draft) => decide(m.id, draft)}
            onEdit={(text) => setEditing(text === null ? null : { id: m.id, text })}
          />
        ))}
      </ul>

      {editable && (
        <div className="sticky bottom-0 -mx-2 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white/95 px-4 py-3 shadow-lg backdrop-blur">
          <div className="text-sm">
            {error ? (
              <Notice tone="error">{error}</Notice>
            ) : undecided > 0 ? (
              <span className="text-amber-800">
                {undecided} {undecided === 1 ? "mapping needs" : "mappings need"} a decision before the build.
              </span>
            ) : (
              <span className="text-slate-700">
                {changes === 0 ? "No changes." : `${changes} ${changes === 1 ? "decision" : "decisions"} ready.`}{" "}
                Everything is decided.
              </span>
            )}
          </div>
          <button
            onClick={submit}
            disabled={undecided > 0 || submitting || editing !== null}
            className="rounded-md bg-indigo-600 px-5 py-2.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {submitting ? "Submitting…" : run.status === "failed" ? "Save and rebuild" : "Submit decisions and build"}
          </button>
        </div>
      )}
    </div>
  );
}

function MappingCard({
  mapping: m,
  column,
  filename,
  draft,
  editable,
  editing,
  onDecide,
  onEdit,
}: {
  mapping: Mapping;
  column: SchemaColumn | undefined;
  filename: string | undefined;
  draft: Draft | undefined;
  editable: boolean;
  editing: string | null; // SQL being edited, or null
  onDecide: (draft: Draft | null) => void;
  onEdit: (text: string | null) => void;
}) {
  const waiting = m.status === "proposed" && !draft;
  const sql = draft?.action === "edit" ? draft.sql : m.transform_sql;
  const toggle = (action: Action) => onDecide(draft?.action === action ? null : { action });

  return (
    <li
      className={`rounded-lg border bg-white p-4 shadow-sm ${
        waiting ? "border-amber-300 ring-1 ring-amber-200" : "border-slate-200"
      } ${draft?.action === "reject" || (!draft && m.status === "rejected") ? "opacity-70" : ""}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-base">
            {m.source_column ? (
              <span className="font-mono">{m.source_column}</span>
            ) : (
              <span className="italic text-rose-700">no matching column in the file</span>
            )}
            <span className="mx-2 text-slate-400">→</span>
            <span className="font-mono font-semibold">{m.target_column}</span>
            {column?.required && (
              <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">required</span>
            )}
          </p>
          {column && <p className="mt-0.5 text-xs text-slate-500">{column.description}</p>}
          {filename && <p className="mt-0.5 text-xs text-slate-500">File: {filename}</p>}
        </div>
        <Confidence value={m.confidence} flagged={m.needs_review} />
      </div>

      <div className="mt-3 grid gap-4 md:grid-cols-[2fr_3fr]">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Most common values</p>
          {m.sample_values.length === 0 ? (
            <p className="mt-1 text-sm text-slate-500">–</p>
          ) : (
            <ul className="mt-1 flex flex-wrap gap-1.5">
              {m.sample_values.map((v) => (
                <li key={v.value} className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs">
                  {v.value === "" ? "(empty)" : v.value}
                  <span className="ml-1 text-slate-500">×{formatNumber(v.count)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Transform SQL{draft?.action === "edit" && <span className="ml-2 normal-case text-indigo-700">(your edit)</span>}
          </p>
          {editing !== null ? (
            <div className="mt-1">
              <textarea
                value={editing}
                onChange={(event) => onEdit(event.target.value)}
                rows={3}
                spellCheck={false}
                className="w-full rounded-md border border-slate-300 p-2 font-mono text-xs"
              />
              <p className="text-xs text-slate-500">
                One DuckDB expression over this row&apos;s columns, e.g. <code>try_cast(col as integer)</code>.
                It is checked when you submit.
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  onClick={() => {
                    onDecide({ action: "edit", sql: editing.trim() });
                    onEdit(null);
                  }}
                  disabled={editing.trim() === ""}
                  className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white disabled:bg-slate-300"
                >
                  Use this SQL
                </button>
                <button onClick={() => onEdit(null)} className="rounded-md px-3 py-1.5 text-xs text-slate-600 underline">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-slate-900 px-3 py-2 font-mono text-xs text-slate-100">
              {sql ?? "–"}
            </pre>
          )}
          <p className="mt-2 text-sm text-slate-700">{m.reason}</p>
          {m.review_reason && <p className="mt-1 text-sm text-amber-800">Sent to review: {m.review_reason}</p>}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-3">
        <span className="text-sm">
          <Status mapping={m} draft={draft} />
        </span>
        {editable && editing === null && (
          <div className="flex gap-2">
            {m.source_column ? (
              <>
                <ActionButton active={draft?.action === "approve"} tone="approve" onClick={() => toggle("approve")}>
                  Approve
                </ActionButton>
                <ActionButton
                  active={draft?.action === "edit"}
                  tone="edit"
                  onClick={() => onEdit(sql ?? "")}
                >
                  Edit
                </ActionButton>
                <ActionButton active={draft?.action === "reject"} tone="reject" onClick={() => toggle("reject")}>
                  Reject
                </ActionButton>
              </>
            ) : (
              <ActionButton active={draft?.action === "approve"} tone="approve" onClick={() => toggle("approve")}>
                Confirm it is missing
              </ActionButton>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function Status({ mapping: m, draft }: { mapping: Mapping; draft: Draft | undefined }) {
  if (draft) {
    return (
      <span className="text-indigo-700">
        Your decision: <strong>{ACTION_LABEL[draft.action]}</strong> (not submitted yet)
      </span>
    );
  }
  switch (m.status) {
    case "proposed":
      return <span className="font-medium text-amber-800">Needs your decision</span>;
    case "approved":
      return m.needs_review ? (
        <span className="text-emerald-700">Approved</span>
      ) : (
        <span className="text-emerald-700">Auto-approved</span>
      );
    case "edited":
      return <span className="text-emerald-700">Edited and approved</span>;
    case "rejected":
      return <span className="text-rose-700">Rejected (column left out)</span>;
  }
}

function Confidence({ value, flagged }: { value: number | null; flagged: boolean }) {
  if (value === null) return <span className="text-sm text-slate-500">no confidence score</span>;
  // The API decides what is low (REVIEW_CONFIDENCE_THRESHOLD); the color follows its routing.
  const color = !flagged ? "bg-emerald-500" : value >= 0.5 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="w-36 text-right">
      <span className="text-sm font-medium tabular-nums">{formatPercent(value)} confident</span>
      <div className="mt-1 h-1.5 overflow-hidden rounded bg-slate-100">
        <div className={`h-full ${color}`} style={{ width: formatPercent(value) }} />
      </div>
    </div>
  );
}

const TONES = {
  approve: ["border-emerald-600 bg-emerald-600 text-white", "border-emerald-600 text-emerald-700 hover:bg-emerald-50"],
  edit: ["border-indigo-600 bg-indigo-600 text-white", "border-indigo-600 text-indigo-700 hover:bg-indigo-50"],
  reject: ["border-rose-600 bg-rose-600 text-white", "border-rose-600 text-rose-700 hover:bg-rose-50"],
} as const;

function ActionButton({
  active,
  tone,
  onClick,
  children,
}: {
  active: boolean;
  tone: keyof typeof TONES;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium ${TONES[tone][active ? 0 : 1]}`}
    >
      {children}
    </button>
  );
}
