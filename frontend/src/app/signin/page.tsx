import { ssoProviderLabel } from "@/app/signin/sso-label";
import { ssoStartUrl } from "@/lib/sign-in";

// Reads the provider label from the server's environment at request time.
export const dynamic = "force-dynamic";

type SearchParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export default async function SignInPage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await props.searchParams;
  const expired = first(params.expired) === "1";
  const label = ssoProviderLabel();

  return (
    <main className="page-shell auth-page">
      <section className="hero-card auth-card">
        <div className="hero-copy">
          <span className="eyebrow">jira2pullreq</span>
          <h1>Sign in</h1>
          <p className="lede">
            Turn a Jira ticket into a reviewed implementation plan, the code
            changes, and a branch ready for review.
          </p>
        </div>

        <section className="panel form-panel">
          {expired ? (
            <p className="banner banner-info">
              Your sign-in has expired. Sign in again to continue where you left
              off.
            </p>
          ) : null}
          <div className="panel-heading">
            <h2>Use your work account</h2>
            <p>
              You&apos;ll continue to {label} to choose your account, enter your
              password, and complete any verification. This app never sees your
              password.
            </p>
          </div>
          <div className="actions">
            <a className="primary-button" href={ssoStartUrl(first(params.rd))}>
              Sign in with {label}
            </a>
          </div>
        </section>
      </section>
    </main>
  );
}
