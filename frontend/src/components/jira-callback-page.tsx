"use client";

import { useEffect, useState } from "react";

import { completeJiraConnect, isAbortError } from "@/lib/api";

type JiraCallbackPageProps = {
  code: string | null;
  state: string | null;
  error: string | null;
  redirectTo?: (href: string) => void;
  redirectDelayMs?: number;
};

function buildJiraResultUrl(params: Record<string, string>): string {
  return `/?${new URLSearchParams(params).toString()}`;
}

export function JiraCallbackPage({
  code,
  state,
  error,
  redirectTo = (href) => window.location.replace(href),
  redirectDelayMs = 900,
}: JiraCallbackPageProps) {
  const [message, setMessage] = useState("Completing Jira sign-in...");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (error) {
      setFailed(true);
      setMessage("Jira sign-in was cancelled or could not be completed.");
      return;
    }
    if (!code || !state) {
      setFailed(true);
      setMessage("The Jira sign-in callback is missing required parameters.");
      return;
    }

    let active = true;
    const controller = new AbortController();

    void (async () => {
      try {
        const response = await completeJiraConnect(
          { code, state },
          controller.signal,
        );
        if (!active) {
          return;
        }
        setMessage(
          `Connected Jira site ${response.connection.site.name}. Returning to the app...`,
        );
        window.setTimeout(() => {
          redirectTo(
            buildJiraResultUrl({
              jira: "connected",
              jira_site: response.connection.site.name,
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
            : "Could not complete Jira sign-in.",
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
            {failed ? "Jira connection failed" : "Finishing Jira connection"}
          </h1>
          <p>{message}</p>
        </div>
        {failed ? (
          <div className="actions">
            <a
              className="secondary-link"
              href={buildJiraResultUrl({ jira: "connect_failed" })}
            >
              Return to app
            </a>
          </div>
        ) : null}
      </section>
    </main>
  );
}
