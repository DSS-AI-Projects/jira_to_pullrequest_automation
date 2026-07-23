import { JiraCallbackPage } from "@/components/jira-callback-page";

export default async function JiraOAuthCallbackPage(props: {
  searchParams: Promise<{
    code?: string;
    state?: string;
    error?: string;
  }>;
}) {
  const searchParams = await props.searchParams;

  return (
    <JiraCallbackPage
      code={searchParams.code ?? null}
      error={searchParams.error ?? null}
      state={searchParams.state ?? null}
    />
  );
}
