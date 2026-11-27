"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Notice } from "@/components/Notice";
import { RunDetails } from "@/components/RunDetails";
import { StatusBadge } from "@/components/StatusBadge";
import { ApiError, api, errorMessage, type Run, type RunStatus } from "@/lib/api";
import { formatNumber, formatTime } from "@/lib/format";

const WORKING: RunStatus[] = ["profiling", "mapping", "building"];
const STEPS = ["Upload", "Profile", "Map", "Review", "Build", "Done"];
const STEP_OF: Partial<Record<RunStatus, number>> = {
  created: 0,
  profiling: 1,
  mapping: 2,
  awaiting_review: 3,
  building: 4,
  done: 5,
};
const WORKING_TEXT: Partial<Record<RunStatus, string>> = {
  profiling: "Profiling the files: counting values, nulls, and formats in every column.",
  mapping: "The agent is matching source columns to the target table and spotting data problems.",
  building: "Building the clean table with dbt and running its tests.",
};

export function RunView({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [pollKey, setPollKey] = useState(0); // bump to fetch now and poll again

  // Poll while the agent works on the run; stop once it waits for a person or has finished.
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const next = await api.run(id);
        if (stopped) return;
        setRun(next);
        setLoadError(null);
        if (next.busy || WORKING.includes(next.status)) timer = window.setTimeout(tick, 2000);
      } catch (err) {
        if (stopped) return;
        setLoadError(errorMessage(err));
        if (!(err instanceof ApiError && err.status === 404)) timer = window.setTimeout(tick, 5000);
      }
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [id, pollKey]);

  const follow = (next: Run) => {
    setRun(next);
    setPollKey((key) => key + 1);
  };

  async function start() {
    setActionError(null);
    setStarting(true);
    try {
      follow(await api.start(id));
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  }

  if (run === null) {
    return loadError ? <Notice tone="error">{loadError}</Notice> : <p className="text-slate-500">Loading…</p>;
  }

  const working = run.busy || WORKING.includes(run.status);
  const rows = run.files.reduce((n, f) => n + f.row_count, 0);

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-sm text-slate-600 hover:underline">
          ← All runs
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold">{run.customer_name}</h1>
          <StatusBadge status={run.status} />
        </div>
        <p className="mt-1 text-sm text-slate-600">
          Target table <span className="font-mono">{run.target_schema}</span> · {run.files.length}{" "}
          {run.files.length === 1 ? "file" : "files"} ({run.files.map((f) => f.filename).join(", ") || "none"}),{" "}
          {formatNumber(rows)} rows · started {formatTime(run.created_at)}
        </p>
      </div>

      <Progress status={run.status} />

      {loadError && <Notice tone="warn">Lost contact with the API, retrying: {loadError}</Notice>}
      {actionError && <Notice tone="error">{actionError}</Notice>}

      {working ? (
        <div className="flex items-center gap-4 rounded-lg border border-sky-200 bg-sky-50 p-5">
          <span className="h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" />
          <div>
            <p className="font-medium">{WORKING_TEXT[run.status] ?? "Starting…"}</p>
            <p className="text-sm text-slate-600">
              {run.steps} model {run.steps === 1 ? "call" : "calls"} so far. This page updates by itself.
            </p>
          </div>
        </div>
      ) : run.status === "created" ? (
        <div className="rounded-lg border border-slate-200 bg-white p-5">
          {run.files.length > 0 ? (
            <>
              <p>The files are uploaded but the agent has not started.</p>
              <button onClick={start} disabled={starting} className={PRIMARY}>
                {starting ? "Starting…" : "Start"}
              </button>
            </>
          ) : (
            <p>
              No files were uploaded to this run. <Link href="/" className="text-indigo-700 underline">Start a new run</Link>.
            </p>
          )}
        </div>
      ) : run.status === "awaiting_review" ? (
        <RunDetails key={run.updated_at} run={run} mode="review" onBuild={follow} />
      ) : run.status === "done" ? (
        <RunDetails key={run.updated_at} run={run} mode="done" onBuild={follow} />
      ) : (
        <>
          <div className="rounded-lg border border-rose-200 bg-rose-50 p-5">
            <p className="font-medium text-rose-900">The run stopped.</p>
            <p className="mt-1 whitespace-pre-wrap text-sm text-rose-900">{run.error}</p>
            <p className="mt-3 text-sm text-slate-700">
              Resume continues from the last step that finished; nothing done so far is lost. If the build failed,
              you can also change mappings below and rebuild.
            </p>
            <button onClick={start} disabled={starting} className={PRIMARY}>
              {starting ? "Resuming…" : "Resume"}
            </button>
          </div>
          <RunDetails key={run.updated_at} run={run} mode="failed" onBuild={follow} />
        </>
      )}
    </div>
  );
}

const PRIMARY =
  "mt-3 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:bg-slate-300";

function Progress({ status }: { status: RunStatus }) {
  const current = STEP_OF[status];
  return (
    <ol className="flex flex-wrap gap-2 text-sm">
      {STEPS.map((step, index) => {
        const state =
          current === undefined ? "idle" : index < current || status === "done" ? "done" : index === current ? "now" : "idle";
        return (
          <li
            key={step}
            className={`rounded-full px-3 py-1 ${
              state === "done"
                ? "bg-emerald-100 text-emerald-800"
                : state === "now"
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-100 text-slate-500"
            }`}
          >
            {index + 1}. {step}
          </li>
        );
      })}
    </ol>
  );
}
