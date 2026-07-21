"use client";

import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import {
  devLogin,
  fetchSession,
  isAbortError,
  logout,
  type SessionInfo,
} from "@/lib/api";

export function AuthGate(props: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();

    void (async () => {
      try {
        const currentSession = await fetchSession(controller.signal);
        if (active) {
          setSession(currentSession);
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
            <div>
              <span className="eyebrow">Signed in</span>
              <strong>{session.user.display_name}</strong>
              <p className="meta-muted">{session.user.email}</p>
            </div>
            <button
              className="secondary-link"
              disabled={submitting}
              onClick={() => void handleLogout()}
              type="button"
            >
              Sign out
            </button>
          </div>
        </header>
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
