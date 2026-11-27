"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";

import { Notice } from "@/components/Notice";
import { api, type ContractOut, errorMessage, type Run } from "@/lib/api";
import { formatNumber, stripExtension } from "@/lib/format";

/** Save a finished onboarding run's approved mappings so the same kind of file can be loaded again. */
export function SaveSourceCard({ run }: { run: Run }) {
  const [name, setName] = useState(() => stripExtension(run.files[0]?.filename ?? "").slice(0, 100));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<ContractOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      setSaved(await api.saveContract(run.id, name.trim()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  if (!saved && run.saved_contract_id !== null) {
    return (
      <Notice tone="ok">
        Saved as a recurring source.{" "}
        <Link href={`/sources/${run.saved_contract_id}`} className="font-medium underline">
          Open the source
        </Link>{" "}
        to load the next file.
      </Notice>
    );
  }

  if (saved) {
    return (
      <Notice tone="ok">
        Saved as source <strong>{saved.source_name}</strong> (version {saved.version})
        {saved.table_rows !== null && `; ${formatNumber(saved.table_rows)} rows are in its table now`}.{" "}
        <Link href={`/sources/${saved.id}`} className="font-medium underline">
          Open the source
        </Link>{" "}
        to load the next file.
      </Notice>
    );
  }

  return (
    <form onSubmit={save} className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold">Save as recurring source</h2>
      <p className="mt-1 text-sm text-slate-600">
        Will {run.customer_name} send this kind of file again? Save it as a source: next time, drop the new file on
        the source&apos;s page and it is checked for changes and loaded with these mappings, without the agent
        starting over.
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-sm font-medium">Source name</span>
          <input
            type="text"
            required
            maxLength={100}
            value={name}
            disabled={saving}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. monthly orders export"
            className="mt-1 block w-72 rounded-md border border-slate-300 px-3 py-2 disabled:bg-slate-50"
          />
        </label>
        <button
          type="submit"
          disabled={saving || name.trim() === ""}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {saving ? "Saving…" : "Save as recurring source"}
        </button>
      </div>
      {error && (
        <div className="mt-3">
          <Notice tone="error">{error}</Notice>
        </div>
      )}
    </form>
  );
}
