import { SourceList } from "@/components/SourceList";

export default function SourcesPage() {
  return (
    <section>
      <h1 className="text-2xl font-semibold">Sources</h1>
      <p className="mt-1 text-slate-600">
        Files a customer sends again and again. Each new file is checked against the saved version: if nothing
        changed it is loaded straight away; if something changed you are asked to review first.
      </p>
      <div className="mt-6">
        <SourceList />
      </div>
    </section>
  );
}
