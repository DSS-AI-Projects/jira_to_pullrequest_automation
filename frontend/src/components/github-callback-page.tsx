"use client";

import { useEffect, useState } from "react";

import { completeGitHubConnect, isAbortError } from "@/lib/api";

type GitHubCallbackPageProps = {
  code: string | null;
  state: string | null;
  error: string | null;
  redirectTo?: (href: string) => void;
  redirectDelayMs?: number;
};

function buildGitHubResultUrl(params: Record<string, string>): string {
  return `/?${new URLSearchParams(params).toString()}`;
}

export function GitHubCallbackPage({
  code,
  state,
  error,
  redirectTo = (href) => window.location.replace(href),
  redirectDelayMs = 900,
}: GitHubCallbackPageProps) {
  const [message, setMessage] = useState("Completing GitHub sign-in...");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (error) {
      setFailed(true);
      setMessage("GitHub sign-in was cancelled or could not be completed.");
      return;
    }
    if (!code || !state) {
      setFailed(true);
      setMessage("The GitHub sign-in callback is missing required parameters.");
      return;
    }

    let active = true;
    const controller = new AbortController();

    void (async () => {
      try {
        const response = await completeGitHubConnect(
          { code, state },
          controller.signal,
        );
        if (!active) {
          return;
        }
        setMessage(
          `Connected GitHub account ${response.connection.account_name}. Returning to the app...`,
        );
        window.setTimeout(() => {
          redirectTo(
            buildGitHubResultUrl({
              github: "connected",
              github_account: response.connection.account_name,
            }),
          );
        }, redirectDelayMs);
      } catch (callbackError) {
        if (!active || isAbortError(callbackError)) {
          return;
        }
        setFailed(true);
        setMessage(
          callbackError instanceof Error
            ? callbackError.message
            : "Could not complete GitHub sign-in.",
        );
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [code, error, redirectDelayMs, redirectTo, state]);

  return (
    <main className="page-shell auth-page">
      <section className="panel stack">
        <div className="panel-heading">
          <h1>
            {failed
              ? "GitHub connection failed"
              : "Finishing GitHub connection"}
          </h1>
          <p>{message}</p>
        </div>
        {failed ? (
          <div className="actions">
            <a
              className="secondary-link"
              href={buildGitHubResultUrl({ github: "connect_failed" })}
            >
              Return to app
            </a>
          </div>
        ) : null}
      </section>
    </main>
  );
}
