"use client";

import { useEffect, useState } from "react";

import { completeGitLabConnect } from "@/lib/api";
import { completeOAuthCallbackOnce } from "@/lib/oauth-callback";

type GitLabCallbackPageProps = {
  code: string | null;
  state: string | null;
  error: string | null;
  redirectTo?: (href: string) => void;
  redirectDelayMs?: number;
};

function buildGitLabResultUrl(params: Record<string, string>): string {
  return `/?${new URLSearchParams(params).toString()}`;
}

export function GitLabCallbackPage({
  code,
  state,
  error,
  redirectTo = (href) => window.location.replace(href),
  redirectDelayMs = 900,
}: GitLabCallbackPageProps) {
  const [message, setMessage] = useState("Completing GitLab sign-in...");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (error) {
      setFailed(true);
      setMessage("GitLab sign-in was cancelled or could not be completed.");
      return;
    }
    if (!code || !state) {
      setFailed(true);
      setMessage("The GitLab sign-in callback is missing required parameters.");
      return;
    }

    let active = true;

    void (async () => {
      try {
        const response = await completeOAuthCallbackOnce(
          `gitlab:${state}`,
          () => completeGitLabConnect({ code, state }),
        );
        if (!active) {
          return;
        }
        setMessage(
          `Connected GitLab account ${response.connection.account_name}. Returning to the app...`,
        );
        window.setTimeout(() => {
          redirectTo(
            buildGitLabResultUrl({
              gitlab: "connected",
              gitlab_account: response.connection.account_name,
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
            : "Could not complete GitLab sign-in.",
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
              ? "GitLab connection failed"
              : "Finishing GitLab connection"}
          </h1>
          <p>{message}</p>
        </div>
        {failed ? (
          <div className="actions">
            <a
              className="secondary-link"
              href={buildGitLabResultUrl({ gitlab: "connect_failed" })}
            >
              Return to app
            </a>
          </div>
        ) : null}
      </section>
    </main>
  );
}
