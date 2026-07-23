import { GitHubCallbackPage } from "@/components/github-callback-page";

export default async function GitHubOAuthCallbackPage(props: {
  searchParams: Promise<{
    code?: string;
    state?: string;
    error?: string;
  }>;
}) {
  const searchParams = await props.searchParams;

  return (
    <GitHubCallbackPage
      code={searchParams.code ?? null}
      error={searchParams.error ?? null}
      state={searchParams.state ?? null}
    />
  );
}
