"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { FileDrop } from "@/components/FileDrop";
import { api, errorMessage, type TargetSchema, uploadFile } from "@/lib/api";
import { formatBytes, formatPercent } from "@/lib/format";

type UploadState = "waiting" | "uploading" | "uploaded" | "rejected";

interface Item {
  key: string;
  file: File;
  state: UploadState;
  progress: number;
  error?: string;
}

export function NewRunForm() {
  const router = useRouter();
  const [schemas, setSchemas] = useState<TargetSchema[] | null>(null);
  const [customer, setCustomer] = useState("");
  const [schema, setSchema] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [runId, setRunId] = useState<string | null>(null); // set once the run exists
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.schemas().then(setSchemas, (err) => setError(errorMessage(err)));
  }, []);

  const addFiles = (files: File[]) =>
    setItems((prev) => {
      const known = new Set(prev.map((item) => item.key));
      const fresh = files
        .map((file) => ({
          key: `${file.name}:${file.size}:${file.lastModified}`,
          file,
          state: "waiting" as const,
          progress: 0,
        }))
        .filter((item) => !known.has(item.key));
      return [...prev, ...fresh];
    });

  const update = (key: string, patch: Partial<Item>) =>
    setItems((prev) => prev.map((item) => (item.key === key ? { ...item, ...patch } : item)));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setWorking(true);
    try {
      const id = runId ?? (await api.createRun(customer.trim(), schema)).id;
      setRunId(id);
      let rejected = 0;
      for (const item of items) {
        if (item.state === "uploaded") continue;
        update(item.key, { state: "uploading", progress: 0, error: undefined });
        try {
          await uploadFile(id, item.file, (progress) => update(item.key, { progress }));
          update(item.key, { state: "uploaded", progress: 1 });
        } catch (err) {
          update(item.key, { state: "rejected", error: errorMessage(err) });
          rejected += 1;
        }
      }
      if (rejected) {
        setError(
          `${rejected === 1 ? "A file was" : `${rejected} files were`} rejected. Remove ${
            rejected === 1 ? "it" : "them"
          } (or fix and add again), then continue.`,
        );
        return;
      }
      await api.start(id);
      router.push(`/runs/${id}`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setWorking(false);
    }
  }

  const locked = runId !== null || working;
  const toUpload = items.filter((item) => item.state !== "uploaded").length;
  const canSubmit =
    !working && customer.trim() !== "" && schema !== "" && items.length > 0 && (toUpload > 0 || runId !== null);

  return (
    <form onSubmit={submit} className="space-y-6 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <label className="block">
        <span className="text-sm font-medium">Customer name</span>
        <input
          type="text"
          required
          maxLength={200}
          value={customer}
          disabled={locked}
          onChange={(event) => setCustomer(event.target.value)}
          placeholder="e.g. Globex Corporation"
          className="mt-1 block w-full max-w-md rounded-md border border-slate-300 px-3 py-2 disabled:bg-slate-50"
        />
      </label>

      <fieldset>
        <legend className="text-sm font-medium">Target table</legend>
        {schemas === null ? (
          <p className="mt-2 text-sm text-slate-500">Loading tables…</p>
        ) : (
          <div className="mt-2 grid gap-3 sm:grid-cols-3">
            {schemas.map((s) => (
              <label
                key={s.table}
                className={`cursor-pointer rounded-lg border p-4 ${
                  schema === s.table ? "border-indigo-500 ring-2 ring-indigo-200" : "border-slate-200"
                } ${locked ? "cursor-not-allowed opacity-70" : ""}`}
              >
                <input
                  type="radio"
                  name="schema"
                  value={s.table}
                  checked={schema === s.table}
                  disabled={locked}
                  onChange={() => setSchema(s.table)}
                  className="sr-only"
                />
                <span className="block font-medium">{s.table}</span>
                <span className="mt-1 block text-sm text-slate-600">{s.description}</span>
                <span className="mt-2 block text-xs text-slate-500">{s.columns.length} columns</span>
              </label>
            ))}
          </div>
        )}
      </fieldset>

      <div>
        <span className="text-sm font-medium">Files</span>
        <div className="mt-2">
          <FileDrop onFiles={addFiles} disabled={working} />
        </div>
        {items.length > 0 && (
          <ul className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
            {items.map((item) => (
              <li key={item.key} className="px-4 py-3 text-sm">
                <div className="flex items-center justify-between gap-4">
                  <span className="font-mono">{item.file.name}</span>
                  <span className="flex items-center gap-3 text-slate-500">
                    {formatBytes(item.file.size)}
                    <FileState item={item} />
                    {item.state !== "uploaded" && item.state !== "uploading" && !working && (
                      <button
                        type="button"
                        onClick={() => setItems((prev) => prev.filter((other) => other.key !== item.key))}
                        className="text-slate-500 underline hover:text-slate-800"
                      >
                        Remove
                      </button>
                    )}
                  </span>
                </div>
                {item.state === "uploading" && (
                  <div className="mt-2 h-1.5 overflow-hidden rounded bg-slate-100">
                    <div className="h-full bg-indigo-500" style={{ width: formatPercent(item.progress) }} />
                  </div>
                )}
                {item.error && <p className="mt-1 text-rose-700">{item.error}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && <p className="rounded-md bg-rose-50 px-4 py-3 text-sm text-rose-800">{error}</p>}

      <div className="flex items-center gap-4">
        <button
          type="submit"
          disabled={!canSubmit}
          className="rounded-md bg-indigo-600 px-5 py-2.5 font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {working ? "Uploading…" : runId ? "Continue" : "Start onboarding"}
        </button>
        {runId && !working && (
          <Link href={`/runs/${runId}`} className="text-sm text-slate-600 underline">
            Go to this run
          </Link>
        )}
      </div>
    </form>
  );
}

function FileState({ item }: { item: Item }) {
  switch (item.state) {
    case "uploading":
      return <span className="text-indigo-700">Uploading {formatPercent(item.progress)}</span>;
    case "uploaded":
      return <span className="text-emerald-700">Uploaded</span>;
    case "rejected":
      return <span className="text-rose-700">Rejected</span>;
    default:
      return null;
  }
}
