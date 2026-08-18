"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { fetchJobs, isAbortError, type JobSummary } from "@/lib/api";
import { JOB_STATE_LABELS } from "@/lib/job";

const PAGE_SIZE = 20;

function statusClass(job: JobSummary): string {
  if (job.state === "FAILED" || job.state === "IMPLEMENTATION_FAILED") {
    return "is-failed";
  }
  if (job.state === "PLAN_READY" || job.state === "IMPLEMENTATION_READY") {
    return "is-passed";
  }
  return "is-skipped";
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString();
}

export function JobListView() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadFirstPage = useCallback(async (signal: AbortSignal) => {
    const response = await fetchJobs({ limit: PAGE_SIZE }, signal);
    setJobs(response.jobs);
    setNextCursor(response.next_cursor);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadFirstPage(controller.signal)
      .catch((loadError: unknown) => {
        if (!isAbortError(loadError)) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load jobs.",
          );
        }
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [loadFirstPage]);

  async function handleLoadMore() {
    if (!nextCursor) {
      return;
    }
    setLoadingMore(true);
    setError(null);
    try {
      const response = await fetchJobs({
        limit: PAGE_SIZE,
        before: nextCursor,
      });
      setJobs((current) => [...current, ...response.jobs]);
      setNextCursor(response.next_cursor);
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Could not load more jobs.",
      );
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Jobs</span>
          <h1>My jobs</h1>
          <p>Every planning job you have submitted, most recent first.</p>
        </div>
        <Link className="secondary-link" href="/">
          New job
        </Link>
      </div>

      {error ? <p className="banner banner-error">{error}</p> : null}

      {loading ? (
        <p className="meta-muted">Loading jobs...</p>
      ) : jobs.length === 0 ? (
        <p className="meta-muted">No jobs yet — start one above.</p>
      ) : (
        <>
          <ul className="content-list">
            {jobs.map((job) => (
              <li key={job.id}>
                <div className="change-header">
                  <Link className="break-all" href={`/jobs/${job.id}`}>
                    <strong>{job.ticket_key}</strong>
                  </Link>
                  <span className={`pill validation-pill ${statusClass(job)}`}>
                    {JOB_STATE_LABELS[job.state]}
                  </span>
                </div>
                <p className="meta-muted break-all">{job.repo_url}</p>
                <p className="meta-muted">
                  Created {formatTimestamp(job.created_at)}
                  {job.error_code ? ` · ${job.error_code}` : ""}
                </p>
                <div className="actions">
                  <Link className="secondary-link" href={`/jobs/${job.id}`}>
                    View details
                  </Link>
                </div>
              </li>
            ))}
          </ul>
          {nextCursor ? (
            <div className="actions">
              <button
                className="pill-button"
                disabled={loadingMore}
                onClick={() => void handleLoadMore()}
                type="button"
              >
                {loadingMore ? "Loading..." : "Load more"}
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
