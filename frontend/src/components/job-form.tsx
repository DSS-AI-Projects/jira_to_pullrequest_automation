"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  createJob,
  fetchRepos,
  isAbortError,
  type RepoChoice,
  type RepoList,
} from "@/lib/api";

const SAMPLE_TICKET = "PROJ-123";
type RepoMode = "remote" | "local";

export function JobForm() {
  const router = useRouter();
  const [ticket, setTicket] = useState("");
  const [repo, setRepo] = useState("");
  const [repoMode, setRepoMode] = useState<RepoMode>("remote");
  const [repos, setRepos] = useState<RepoChoice[]>([]);
  const [allowedHosts, setAllowedHosts] = useState<string[]>([]);
  const [localRepoSupport, setLocalRepoSupport] =
    useState<RepoList["local_repo_support"] | null>(null);
  const [loadingRepos, setLoadingRepos] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();

    void (async () => {
      try {
        const response = await fetchRepos(controller.signal);
        if (active) {
          setRepos(response.repos);
          setAllowedHosts(response.allowed_hosts);
          setLocalRepoSupport(response.local_repo_support);
        }
      } catch (repoError) {
        if (!active || isAbortError(repoError)) {
          return;
        }
        setError(
          repoError instanceof Error
            ? repoError.message
            : "Could not load repositories.",
        );
      } finally {
        if (active) {
          setLoadingRepos(false);
        }
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  const repoHelper = useMemo(() => {
    if (repoMode === "local") {
      return "Enter an approved absolute local path to a Git working tree.";
    }
    if (repos.length === 0) {
      return "Enter an allowed repository URL or pre-configured repo name.";
    }
    return "Pick a pre-configured repo below or enter an allowed repository URL.";
  }, [repoMode, repos.length]);

  const repoLabel =
    repoMode === "local"
      ? "Local repository path"
      : "Repository URL or pre-configured name";

  const repoPlaceholder =
    repoMode === "local"
      ? String.raw`D:\repos\my-service`
      : "hello-world-sample or https://github.com/org/repo";

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    void (async () => {
      try {
        const result = await createJob({
          ticket,
          repo,
        });
        router.push(`/jobs/${result.job_id}`);
      } catch (submitError) {
        setError(
          submitError instanceof Error
            ? submitError.message
            : "Could not create the job.",
        );
      } finally {
        setSubmitting(false);
      }
    })();
  }

  return (
    <section className="panel form-panel">
      <div className="panel-heading">
        <h2>Start a planning job</h2>
        <p>
          Submit only a Jira identifier and repository identifier. Jira, git,
          and Anthropic credentials stay on the server.
        </p>
      </div>

      <form className="job-form" onSubmit={handleSubmit}>
        <label className="field">
          <span>Jira ticket key or URL</span>
          <input
            autoComplete="off"
            className="text-input"
            name="ticket"
            onChange={(event) => setTicket(event.target.value)}
            placeholder={SAMPLE_TICKET}
            required
            value={ticket}
          />
          <small>Example: {SAMPLE_TICKET} or your Jira ticket URL.</small>
        </label>

        <label className="field">
          <span>Repository source</span>
          <div className="source-toggle" role="tablist" aria-label="Repository source">
            <button
              aria-selected={repoMode === "remote"}
              className={["pill-button", repoMode === "remote" ? "is-selected" : ""]
                .filter(Boolean)
                .join(" ")}
              onClick={() => {
                setRepoMode("remote");
                setRepo("");
              }}
              type="button"
            >
              Remote or pre-configured
            </button>
            <button
              aria-selected={repoMode === "local"}
              className={["pill-button", repoMode === "local" ? "is-selected" : ""]
                .filter(Boolean)
                .join(" ")}
              disabled={!localRepoSupport?.enabled}
              onClick={() => {
                setRepoMode("local");
                setRepo("");
              }}
              type="button"
            >
              Local repo path
            </button>
          </div>
          {!localRepoSupport?.enabled ? (
            <small>Local repo mode is not enabled on this server.</small>
          ) : null}
        </label>

        <label className="field">
          <span>{repoLabel}</span>
          <input
            autoComplete="off"
            className="text-input"
            list="repo-suggestions"
            name="repo"
            onChange={(event) => setRepo(event.target.value)}
            placeholder={repoPlaceholder}
            required
            value={repo}
          />
          <small>{repoHelper}</small>
        </label>

        <datalist id="repo-suggestions">
          {repos.map((repoOption) => (
            <option key={repoOption.name} value={repoOption.name}>
              {repoOption.url}
            </option>
          ))}
        </datalist>

        {repoMode === "remote" && repos.length > 0 ? (
          <div className="quick-picks">
            <span className="quick-picks-label">Pre-configured repos</span>
            <div className="pill-row">
              {repos.map((repoOption) => (
                <button
                  className="pill-button"
                  key={repoOption.name}
                  onClick={() => setRepo(repoOption.name)}
                  type="button"
                >
                  {repoOption.name}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="meta-block">
          <span className="meta-label">Allowed hosts</span>
          <div className="pill-row">
            {allowedHosts.map((host) => (
              <span className="pill" key={host}>
                {host}
              </span>
            ))}
            {allowedHosts.length === 0 && !loadingRepos ? (
              <span className="meta-muted">
                No hosts are currently configured.
              </span>
            ) : null}
          </div>
        </div>

        {localRepoSupport?.enabled ? (
          <div className="meta-block">
            <span className="meta-label">Local repo policy</span>
            <div className="pill-row">
              {localRepoSupport.allowed_roots.map((root) => (
                <span className="pill break-all" key={root}>
                  {root}
                </span>
              ))}
              {localRepoSupport.allowed_roots.length === 0 ? (
                <span className="meta-muted">No local roots configured.</span>
              ) : null}
            </div>
            <div className="policy-list">
              <span>
                Dirty repos:{" "}
                {localRepoSupport.allow_dirty ? "allowed by config" : "rejected by default"}
              </span>
              <span>
                Branch match:{" "}
                {localRepoSupport.require_ticket_branch_match
                  ? "must include the Jira key"
                  : "not enforced"}
              </span>
            </div>
          </div>
        ) : null}

        <div className="meta-note">
          <strong>Security:</strong> this app never accepts secrets in the form.
          If a value looks like a token, the backend will reject it.
        </div>

        {error ? <p className="banner banner-error">{error}</p> : null}

        <div className="actions">
          <button
            className="primary-button"
            disabled={submitting || loadingRepos}
            type="submit"
          >
            {submitting ? "Starting job..." : "Generate implementation plan"}
          </button>
          <Link
            className="secondary-link"
            href="https://github.com/octocat/Hello-World"
          >
            Sample repository
          </Link>
        </div>
      </form>
    </section>
  );
}
