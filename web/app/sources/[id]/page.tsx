import { SourceView } from "@/components/SourceView";

export default async function SourcePage({ params }: PageProps<"/sources/[id]">) {
  const { id } = await params;
  return <SourceView id={id} />;
}
