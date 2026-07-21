import { JobStatusView } from "@/components/job-status-view";

export default async function JobPage(props: {
  params: Promise<{ jobId: string }>;
}) {
  const params = await props.params;

  return (
    <main className="page-shell">
      <JobStatusView jobId={params.jobId} />
    </main>
  );
}
