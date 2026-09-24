"use client";

import { useEffect, useState } from "react";

import { completeBitbucketConnect } from "@/lib/api";
import { completeOAuthCallbackOnce } from "@/lib/oauth-callback";

type BitbucketCallbackPageProps = {
  code: string | null;
  state: string | null;
  error: string | null;
  redirectTo?: (href: string) => void;
  redirectDelayMs?: number;
};

function buildBitbucketResultUrl(params: Record<string, string>): string {
  return `/?${new URLSearchParams(params).toString()}`;
}

export function BitbucketCallbackPage({
  code,
  state,
  error,
  redirectTo = (href) => window.location.replace(href),
  redirectDelayMs = 900,
}: BitbucketCallbackPageProps) {
  const [message, setMessage] = useState("Completing Bitbucket sign-in...");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (error) {
      setFailed(true);
      setMessage("Bitbucket sign-in was cancelled or could not be completed.");
      return;
    }
    if (!code || !state) {
      setFailed(true);
      setMessage(
        "The Bitbucket sign-in callback is missing required parameters.",
      );
      return;
    }

    let active = true;

    void (async () => {
      try {
        const response = await completeOAuthCallbackOnce(
          `bitbucket:${state}`,
          () => completeBitbucketConnect({ code, state }),
        );
        if (!active) {
          return;
        }
        setMessage(
          `Connected Bitbucket account ${response.connection.account_name}. Returning to the app...`,
        );
        window.setTimeout(() => {
          redirectTo(
            buildBitbucketResultUrl({
              bitbucket: "connected",
              bitbucket_account: response.connection.account_name,
            }),
          );
        }, redirectDelayMs);
      } catch (callbackError) {
        if (!active) {
          return;
        }
        setFailed(true);
        setMessage(
          callbackError instanceof Error
            ? callbackError.message
            : "Could not complete Bitbucket sign-in.",
        );
      }
    })();

    return () => {
      active = false;
    };
  }, [code, error, redirectDelayMs, redirectTo, state]);

  return (
    <main className="page-shell auth-page">
      <section className="panel stack">
        <div className="panel-heading">
          <h1>
            {failed
              ? "Bitbucket connection failed"
              : "Finishing Bitbucket connection"}
          </h1>
          <p>{message}</p>
        </div>
        {failed ? (
          <div className="actions">
            <a
              className="secondary-link"
              href={buildBitbucketResultUrl({ bitbucket: "connect_failed" })}
            >
              Return to app
            </a>
          </div>
        ) : null}
      </section>
    </main>
  );
}
