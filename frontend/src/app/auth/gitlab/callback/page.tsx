import { GitLabCallbackPage } from "@/components/gitlab-callback-page";

export default async function GitLabOAuthCallbackPage(props: {
  searchParams: Promise<{
    code?: string;
    state?: string;
    error?: string;
  }>;
}) {
  const searchParams = await props.searchParams;

  return (
    <GitLabCallbackPage
      code={searchParams.code ?? null}
      error={searchParams.error ?? null}
      state={searchParams.state ?? null}
    />
  );
}
