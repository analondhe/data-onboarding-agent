import { NewRunForm } from "@/components/NewRunForm";
import { RecentRuns } from "@/components/RecentRuns";

export default function Home() {
  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-2xl font-semibold">New run</h1>
        <p className="mt-1 text-slate-600">
          Upload a customer&apos;s files. The agent studies them, proposes how each column maps to
          the target table, and asks you to check anything it is unsure about.
        </p>
        <div className="mt-6">
          <NewRunForm />
        </div>
      </section>
      <section>
        <h2 className="text-xl font-semibold">Recent runs</h2>
        <div className="mt-4">
          <RecentRuns />
        </div>
      </section>
    </div>
  );
}
