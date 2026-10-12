import { ssoProviderLabel } from "@/app/signin/sso-label";
import { SIGN_IN_PATH } from "@/lib/sign-in";

// Reads the provider label from the server's environment at request time.
export const dynamic = "force-dynamic";

// Where the identity provider returns the browser after Sign out. Its URL must
// be registered with the provider as a post-logout redirect.
export default function SignedOutPage() {
  const label = ssoProviderLabel();

  return (
    <main className="page-shell auth-page">
      <section className="hero-card auth-card">
        <div className="hero-copy">
          <span className="eyebrow">jira2pullreq</span>
          <h1>You have signed out</h1>
          <p className="lede">
            You&apos;re signed out of jira2pullreq and of your {label} account
            in this browser.
          </p>
        </div>

        <section className="panel form-panel">
          <div className="panel-heading">
            <h2>Shared computer?</h2>
            <p>Close the browser to make sure no other session stays open.</p>
          </div>
          <div className="actions">
            <a className="primary-button" href={SIGN_IN_PATH}>
              Sign in again
            </a>
          </div>
        </section>
      </section>
    </main>
  );
}
