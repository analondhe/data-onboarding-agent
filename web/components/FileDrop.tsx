"use client";

import { useRef, useState } from "react";

const ACCEPTED = [".csv", ".tsv", ".txt", ".parquet"];

export function FileDrop({ onFiles, disabled }: { onFiles: (files: File[]) => void; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  const choose = () => {
    if (!disabled) input.current?.click();
  };

  return (
    <>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={choose}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            choose();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          if (!disabled) onFiles(Array.from(event.dataTransfer.files));
        }}
        className={`flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-10 text-center transition ${
          disabled
            ? "cursor-not-allowed border-slate-200 bg-slate-50 text-slate-400"
            : over
              ? "cursor-copy border-indigo-500 bg-indigo-50"
              : "cursor-pointer border-slate-300 bg-white hover:border-indigo-400"
        }`}
      >
        <p className="font-medium">Drop files here, or click to choose</p>
        <p className="mt-1 text-sm text-slate-500">CSV, TSV, TXT or Parquet · several files allowed</p>
      </div>
      <input
        ref={input}
        type="file"
        multiple
        accept={ACCEPTED.join(",")}
        className="hidden"
        data-testid="file-input"
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
    </>
  );
}
