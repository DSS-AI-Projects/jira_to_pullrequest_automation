"use client";

import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import {
  disconnectJira,
  disconnectRepoHostingProvider,
  devLogin,
  fetchJiraAuthStatus,
  fetchRepoHostingStatus,
  fetchSession,
  isAbortError,
  logout,
  redirectBrowser,
  startGitHubConnect,
  startJiraConnect,
  type JiraAuthStatus,
  type RepoHostingProvider,
  type RepoHostingStatus,
  type SessionInfo,
} from "@/lib/api";

type JiraFlash = {
  kind: "success" | "error";
  message: string;
};

function readConnectionFlash(): JiraFlash | null {
  if (typeof window === "undefined") {
    return null;
  }

  const params = new URLSearchParams(window.location.search);
  const jira = params.get("jira");
  if (jira === "connected") {
    const siteName = params.get("jira_site");
    return {
      kind: "success",
      message: siteName
        ? `Connected Jira site ${siteName}.`
        : "Connected your Jira account.",
    };
  }
  if (jira === "connect_failed") {
    return {
      kind: "error",
      message: "Jira sign-in did not complete. Try connecting your Jira account again.",
    };
  }
  const github = params.get("github");
  if (github === "connected") {
    const accountName = params.get("github_account");
    return {
      kind: "success",
      message: accountName
        ? `Connected GitHub account ${accountName}.`
        : "Connected your GitHub account.",
    };
  }
  if (github === "connect_failed") {
    return {
      kind: "error",
      message: "GitHub sign-in did not complete. Try connecting your GitHub account again.",
    };
  }
  return null;
}

export function AuthGate(props: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [jiraStatus, setJiraStatus] = useState<JiraAuthStatus | null>(null);
  const [repoHostingStatus, setRepoHostingStatus] = useState<RepoHostingStatus | null>(null);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [jiraSubmitting, setJiraSubmitting] = useState(false);
  const [repoSubmitting, setRepoSubmitting] = useState<RepoHostingProvider | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jiraFlash, setJiraFlash] = useState<JiraFlash | null>(readConnectionFlash);

  useEffect(() => {
    if (!jiraFlash) {
      return;
    }
    const timeout = window.setTimeout(() => setJiraFlash(null), 8000);
    return () => window.clearTimeout(timeout);
  }, [jiraFlash]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const params = new URLSearchParams(window.location.search);
    if (!params.get("jira") && !params.get("github")) {
      return;
    }
    params.delete("jira");
    params.delete("jira_site");
    params.delete("github");
    params.delete("github_account");
    const nextSearch = params.toString();
    const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`;
    window.history.replaceState({}, "", nextUrl);
  }, []);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();

    async function loadJiraStatus(signal?: AbortSignal) {
      const status = await fetchJiraAuthStatus(signal);
      if (active) {
        setJiraStatus(status);
      }
    }

    async function loadRepoHostingStatus(signal?: AbortSignal) {
      const status = await fetchRepoHostingStatus(signal);
      if (active) {
        setRepoHostingStatus(status);
      }
    }

    void (async () => {
      try {
        const currentSession = await fetchSession(controller.signal);
        if (active) {
          setSession(currentSession);
        }
        if (currentSession.auth_enabled && currentSession.user) {
          await loadJiraStatus(controller.signal);
          await loadRepoHostingStatus(controller.signal);
        } else if (active) {
          setJiraStatus(null);
          setRepoHostingStatus(null);
        }
      } catch (sessionError) {
        if (!active || isAbortError(sessionError)) {
          return;
        }
        setError(
          sessionError instanceof Error
            ? sessionError.message
            : "Could not load the current session.",
        );
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  async function handleLogin(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const currentSession = await devLogin({
        email,
        display_name: displayName,
      });
      setSession(currentSession);
      if (currentSession.auth_enabled && currentSession.user) {
        setJiraStatus(await fetchJiraAuthStatus());
        setRepoHostingStatus(await fetchRepoHostingStatus());
      } else {
        setJiraStatus(null);
        setRepoHostingStatus(null);
      }
      setDisplayName("");
      setEmail("");
    } catch (loginError) {
      setError(
        loginError instanceof Error
          ? loginError.message
          : "Could not start a session.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleLogout() {
    setSubmitting(true);
    setError(null);
    try {
      await logout();
      const currentSession = await fetchSession();
      setSession(currentSession);
      setJiraStatus(null);
      setRepoHostingStatus(null);
    } catch (logoutError) {
      setError(
        logoutError instanceof Error
          ? logoutError.message
          : "Could not sign out.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleJiraConnect() {
    setJiraFlash(null);
    setJiraSubmitting(true);
    setError(null);
    try {
      const response = await startJiraConnect();
      redirectBrowser(response.authorization_url);
    } catch (jiraError) {
      setError(
        jiraError instanceof Error
          ? jiraError.message
          : "Could not start Jira sign-in.",
      );
      setJiraSubmitting(false);
    }
  }

  async function handleJiraDisconnect() {
    setJiraSubmitting(true);
    setError(null);
    try {
      await disconnectJira();
      setJiraStatus(await fetchJiraAuthStatus());
    } catch (jiraError) {
      setError(
        jiraError instanceof Error
          ? jiraError.message
          : "Could not disconnect Jira.",
      );
    } finally {
      setJiraSubmitting(false);
    }
  }

  async function handleRepoProviderDisconnect(provider: RepoHostingProvider) {
    setRepoSubmitting(provider);
    setError(null);
    try {
      await disconnectRepoHostingProvider(provider);
      setRepoHostingStatus(await fetchRepoHostingStatus());
    } catch (providerError) {
      setError(
        providerError instanceof Error
          ? providerError.message
          : "Could not disconnect the repository provider.",
      );
    } finally {
      setRepoSubmitting(null);
    }
  }

  async function handleRepoProviderConnect(provider: RepoHostingProvider) {
    setJiraFlash(null);
    setRepoSubmitting(provider);
    setError(null);
    try {
      if (provider === "GITHUB") {
        const response = await startGitHubConnect();
        redirectBrowser(response.authorization_url);
        return;
      }
      setError("Connect flow for this repository provider is not available yet.");
      setRepoSubmitting(null);
    } catch (providerError) {
      setError(
        providerError instanceof Error
          ? providerError.message
          : "Could not start the repository provider sign-in.",
      );
      setRepoSubmitting(null);
    }
  }

  function renderJiraStatus() {
    if (!session?.auth_enabled || !session.user) {
      return null;
    }
    if (loading) {
      return null;
    }
    if (!jiraStatus) {
      return (
        <div className="jira-access">
          <span className="eyebrow">Jira access</span>
          <p className="meta-muted">Loading Jira access...</p>
        </div>
      );
    }

    if (jiraStatus.connected && jiraStatus.connection) {
      return (
        <div className="jira-access">
          <span className="eyebrow">Jira access</span>
          <strong>Connected to {jiraStatus.connection.site.name}</strong>
          <p className="meta-muted">{jiraStatus.connection.site.url}</p>
          <p className="meta-muted">
            Your personal Jira connection is active for ticket fetches.
          </p>
          <div className="actions">
            <button
              className="secondary-link"
              disabled={jiraSubmitting}
              onClick={() => void handleJiraDisconnect()}
              type="button"
            >
              {jiraSubmitting ? "Disconnecting..." : "Disconnect Jira"}
            </button>
          </div>
        </div>
      );
    }

    if (jiraStatus.oauth_configured) {
      return (
        <div className="jira-access">
          <span className="eyebrow">Jira access</span>
          <strong>Personal Jira not connected</strong>
          <p className="meta-muted">
            {jiraStatus.shared_configured
              ? "The server can still use shared Jira credentials until you connect your own Jira account."
              : "Connect your Jira account so ticket fetches use your own access."}
          </p>
          <div className="actions">
            <button
              className="secondary-link"
              disabled={jiraSubmitting}
              onClick={() => void handleJiraConnect()}
              type="button"
            >
              {jiraSubmitting ? "Opening Jira..." : "Connect Jira"}
            </button>
          </div>
        </div>
      );
    }

    if (jiraStatus.shared_configured) {
      return (
        <div className="jira-access">
          <span className="eyebrow">Jira access</span>
          <strong>Using shared Jira access</strong>
          <p className="meta-muted">
            This environment is currently using the server&apos;s shared Jira credentials.
          </p>
        </div>
      );
    }

    return (
      <div className="jira-access">
        <span className="eyebrow">Jira access</span>
        <strong>Jira is not configured</strong>
        <p className="meta-muted">
          Configure shared Jira credentials or enable delegated Jira OAuth for this environment.
        </p>
      </div>
    );
  }

  function renderRepoHostingStatus() {
    if (!session?.auth_enabled || !session.user) {
      return null;
    }
    if (loading) {
      return null;
    }
    if (!repoHostingStatus) {
      return (
        <div className="jira-access">
          <span className="eyebrow">Repository access</span>
          <p className="meta-muted">Loading repository provider status...</p>
        </div>
      );
    }

    return (
      <div className="jira-access">
        <span className="eyebrow">Repository access</span>
        {repoHostingStatus.providers.map((provider) => (
          <div className="provider-access" key={provider.provider}>
            <strong>{provider.display_name}</strong>
            {provider.connected && provider.connection ? (
              <>
                <p className="meta-muted">
                  Connected as{" "}
                  <a href={provider.connection.account_url} rel="noreferrer" target="_blank">
                    {provider.connection.account_name}
                  </a>
                </p>
                <div className="actions">
                  <button
                    className="secondary-link"
                    disabled={repoSubmitting === provider.provider}
                    onClick={() => void handleRepoProviderDisconnect(provider.provider)}
                    type="button"
                  >
                    {repoSubmitting === provider.provider
                      ? `Disconnecting ${provider.display_name}...`
                      : `Disconnect ${provider.display_name}`}
                  </button>
                </div>
              </>
            ) : provider.configured ? (
              <>
                <p className="meta-muted">
                  {provider.display_name} is configured on this server but not connected for this
                  user yet.
                </p>
                <div className="actions">
                  <button
                    className="secondary-link"
                    disabled={repoSubmitting === provider.provider}
                    onClick={() => void handleRepoProviderConnect(provider.provider)}
                    type="button"
                  >
                    {repoSubmitting === provider.provider
                      ? `Opening ${provider.display_name}...`
                      : `Connect ${provider.display_name}`}
                  </button>
                </div>
                {provider.provider !== "GITHUB" ? (
                  <p className="meta-muted">
                    The connection flow for {provider.display_name} will be added in a later slice.
                  </p>
                ) : null}
              </>
            ) : provider.enabled ? (
              <p className="meta-muted">
                {provider.display_name} is enabled but still missing required server config.
              </p>
            ) : (
              <p className="meta-muted">
                {provider.display_name} connections are not enabled in this environment.
              </p>
            )}
          </div>
        ))}
      </div>
    );
  }

  if (loading) {
    return (
      <main className="page-shell">
        <section className="panel">
          <p>Loading session...</p>
        </section>
      </main>
    );
  }

  if (session?.auth_enabled && !session.user) {
    return (
      <main className="page-shell auth-page">
        <section className="hero-card auth-card">
          <div className="hero-copy">
            <span className="eyebrow">Secure access</span>
            <h1>Sign in to continue</h1>
            <p className="lede">
              Authentication is enabled on this server. Sign in before you
              create or inspect jobs so the app can enforce per-user ownership.
            </p>
          </div>

          <section className="panel form-panel">
            <div className="panel-heading">
              <h2>Session</h2>
              <p>
                {session.can_dev_login
                  ? "Use development login locally, or replace it with your upstream SSO flow."
                  : "Use the configured upstream identity provider or trusted proxy."}
              </p>
            </div>

            {session.can_dev_login ? (
              <form
                className="job-form"
                onSubmit={(event) => {
                  void handleLogin(event);
                }}
              >
                <label className="field">
                  <span>Email</span>
                  <input
                    autoComplete="email"
                    className="text-input"
                    name="email"
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="sam@example.com"
                    required
                    type="email"
                    value={email}
                  />
                </label>

                <label className="field">
                  <span>Display name</span>
                  <input
                    autoComplete="name"
                    className="text-input"
                    name="displayName"
                    onChange={(event) => setDisplayName(event.target.value)}
                    placeholder="Sam"
                    required
                    value={displayName}
                  />
                </label>

                {error ? <p className="banner banner-error">{error}</p> : null}

                <div className="actions">
                  <button className="primary-button" disabled={submitting} type="submit">
                    {submitting ? "Signing in..." : "Sign in"}
                  </button>
                </div>
              </form>
            ) : (
              <>
                {error ? <p className="banner banner-error">{error}</p> : null}
                <p className="meta-muted">
                  No local sign-in fallback is enabled for this environment.
                </p>
              </>
            )}
          </section>
        </section>
      </main>
    );
  }

  return (
    <>
      {session?.auth_enabled && session.user ? (
        <header className="auth-bar">
          <div className="auth-bar__content">
            <div className="auth-bar__identity">
              <div>
                <span className="eyebrow">Signed in</span>
                <strong>{session.user.display_name}</strong>
                <p className="meta-muted">{session.user.email}</p>
              </div>
              {renderJiraStatus()}
              {renderRepoHostingStatus()}
            </div>
            <div className="auth-bar__actions">
              <button
                className="secondary-link"
                disabled={submitting || jiraSubmitting || repoSubmitting !== null}
                onClick={() => void handleLogout()}
                type="button"
              >
                Sign out
              </button>
            </div>
          </div>
        </header>
      ) : null}
      {jiraFlash ? (
        <main className="page-shell">
          <section className="panel">
            <p
              className={`banner ${jiraFlash.kind === "success" ? "banner-info" : "banner-error"}`}
            >
              {jiraFlash.message}
            </p>
          </section>
        </main>
      ) : null}
      {error ? (
        <main className="page-shell">
          <section className="panel">
            <p className="banner banner-error">{error}</p>
          </section>
        </main>
      ) : null}
      {props.children}
    </>
  );
}
