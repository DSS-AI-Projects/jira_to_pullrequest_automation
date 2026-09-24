import { BitbucketCallbackPage } from "@/components/bitbucket-callback-page";

export default async function BitbucketOAuthCallbackPage(props: {
  searchParams: Promise<{
    code?: string;
    state?: string;
    error?: string;
  }>;
}) {
  const searchParams = await props.searchParams;

  return (
    <BitbucketCallbackPage
      code={searchParams.code ?? null}
      error={searchParams.error ?? null}
      state={searchParams.state ?? null}
    />
  );
}
