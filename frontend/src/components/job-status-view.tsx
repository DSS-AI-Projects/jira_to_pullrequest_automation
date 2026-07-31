"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  fetchJob,
  implementJob,
  isAbortError,
  type Job,
  type ImplementationDiffFile,
  type ValidationResult,
} from "@/lib/api";
import { isTerminalState, JOB_STATES, JOB_STATE_LABELS } from "@/lib/job";

import { PlanView } from "./plan-view";

const POLL_INTERVAL_MS = 2000;

function currency(value: number | null): string {
  if (value === null) {
    return "Not recorded";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function totalCost(job: Job): number | null {
  const values = [
    job.usage?.total_cost_usd,
    job.implementation_usage?.total_cost_usd,
  ].filter((value): value is number => value !== null && value !== undefined);
  if (values.length === 0) {
    return null;
  }
  return values.reduce((sum, value) => sum + value, 0);
}

function diffStat(file: ImplementationDiffFile): string {
  const additions = file.additions ?? 0;
  const deletions = file.deletions ?? 0;
  if (file.is_binary) {
    return "Binary";
  }
  if (additions === 0 && deletions === 0) {
    return "No line changes";
  }
  return `+${additions} -${deletions}`;
}

export function JobStatusView(props: { jobId: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [implementing, setImplementing] = useState(false);
  const [clarifications, setClarifications] = useState("");

  const loadJob = useCallback(
    async (signal: AbortSignal) => {
      const currentJob = await fetchJob(props.jobId, signal);
      setJob(currentJob);
      setError(null);
      return currentJob;
    },
    [props.jobId],
  );

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();

    async function load() {
      try {
        const currentJob = await loadJob(controller.signal);
        if (cancelled) {
          return;
        }

        if (!isTerminalState(currentJob.state)) {
          timeoutId = setTimeout(() => {
            void load();
          }, POLL_INTERVAL_MS);
        }
      } catch (jobError) {
        if (!cancelled && !isAbortError(jobError)) {
          setError(
            jobError instanceof Error
              ? jobError.message
              : "Could not load job status.",
          );
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
      controller.abort();
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, [loadJob, refreshKey]);

  const activeIndex = useMemo(() => {
    if (!job) {
      return 0;
    }
    if (job.state === "FAILED" || job.state === "IMPLEMENTATION_FAILED") {
      return JOB_STATES.findIndex((state) => state === job.error?.stage);
    }
    return JOB_STATES.findIndex((state) => state === job.state);
  }, [job]);

  const canImplement =
    job?.state === "PLAN_READY" && job.repo_info?.source_kind === "LOCAL";

  const implementationResult =
    job?.state === "IMPLEMENTATION_READY" ? job.implementation_result : null;

  async function handleImplement() {
    if (!job) {
      return;
    }
    setImplementing(true);
    setError(null);
    try {
      await implementJob(job.id, { clarifications });
      setRefreshKey((value) => value + 1);
    } catch (implementError) {
      setError(
        implementError instanceof Error
          ? implementError.message
          : "Could not start implementation.",
      );
    } finally {
      setImplementing(false);
    }
  }

  return (
    <div className="job-layout">
      <section className="panel status-header">
        <div className="status-heading">
          <div>
            <span className="eyebrow">Job status</span>
            <h1>Planning job {props.jobId}</h1>
            <p>
              Review the generated plan, approve implementation when ready, and
              track implementation plus validation through to completion.
            </p>
          </div>
          <Link className="secondary-link" href="/">
            New job
          </Link>
        </div>

        {error ? <p className="banner banner-error">{error}</p> : null}

        <ol className="status-timeline">
          {JOB_STATES.map((state, index) => {
            const isActive = index === activeIndex;
            const isComplete = job
              ? index < activeIndex || job.state === "IMPLEMENTATION_READY"
              : false;
            return (
              <li
                className={[
                  "timeline-item",
                  isActive ? "is-active" : "",
                  isComplete ? "is-complete" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                key={state}
              >
                <span className="timeline-badge">{index + 1}</span>
                <div>
                  <strong>{JOB_STATE_LABELS[state]}</strong>
                  {job?.state === state ? <p>Current step</p> : null}
                  {job?.state === "FAILED" && job.error?.stage === state ? (
                    <p>Failed here</p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      </section>

      {job ? (
        <section className="panel summary-grid">
          <div className="summary-card">
            <span className="meta-label">Ticket</span>
            <strong>{job.ticket_key}</strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">Repository</span>
            <strong className="break-all">{job.repo_url}</strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">State</span>
            <strong>{JOB_STATE_LABELS[job.state]}</strong>
          </div>
          {job.repo_info ? (
            <>
              <div className="summary-card">
                <span className="meta-label">Source</span>
                <strong>
                  {job.repo_info.source_kind === "LOCAL"
                    ? "Local repo"
                    : "Remote repo"}
                </strong>
              </div>
              <div className="summary-card">
                <span className="meta-label">Branch</span>
                <strong className="break-all">{job.repo_info.branch}</strong>
              </div>
              <div className="summary-card">
                <span className="meta-label">Commit</span>
                <strong className="break-all">
                  {job.repo_info.commit_sha.slice(0, 12)}
                </strong>
              </div>
              <div className="summary-card">
                <span className="meta-label">Working tree</span>
                <strong>{job.repo_info.is_dirty ? "Dirty" : "Clean"}</strong>
              </div>
            </>
          ) : null}
        </section>
      ) : null}

      {job?.error ? (
        <section className="panel">
          <h2>Job failed</h2>
          <p className="banner banner-error">
            {job.error.code}: {job.error.message}
          </p>
        </section>
      ) : null}

      {job?.plan ? (
        <PlanView
          plan={job.plan}
          usage={job.usage}
          planningNotes={job.planning_notes}
        />
      ) : null}

      {job?.repo_info && job?.plan ? (
        <section className="panel">
          <h2>Repository details</h2>
          <dl className="detail-grid">
            {job.repo_info.local_path ? (
              <>
                <dt>Original local path</dt>
                <dd className="break-all">{job.repo_info.local_path}</dd>
              </>
            ) : null}
            {job.workspace_path ? (
              <>
                <dt>Implementation workspace</dt>
                <dd className="break-all">{job.workspace_path}</dd>
              </>
            ) : null}
            {job.repo_info.origin_url ? (
              <>
                <dt>Origin</dt>
                <dd className="break-all">{job.repo_info.origin_url}</dd>
              </>
            ) : null}
          </dl>
        </section>
      ) : null}

      {job?.plan ? (
        <section className="panel">
          <h2>Cost summary</h2>
          <div className="summary-grid">
            <div className="summary-card">
              <span className="meta-label">Planning cost</span>
              <strong>{currency(job.usage?.total_cost_usd ?? null)}</strong>
            </div>
            <div className="summary-card">
              <span className="meta-label">Implementation cost</span>
              <strong>
                {currency(job.implementation_usage?.total_cost_usd ?? null)}
              </strong>
            </div>
            <div className="summary-card">
              <span className="meta-label">Total cost</span>
              <strong>{currency(totalCost(job))}</strong>
            </div>
          </div>
        </section>
      ) : null}

      {job?.plan ? (
        <section className="panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Implementation</span>
              <h2>Approve execution</h2>
              <p>
                Start implementation only after you are satisfied with the plan
                details above.
              </p>
            </div>
          </div>
          {canImplement ? (
            <div className="stack">
              <label className="field">
                <span>Additional guidance (optional)</span>
                <small>
                  Answer any open questions from the plan, or add other context
                  the implementation should take into account.
                </small>
                <textarea
                  className="text-input"
                  rows={4}
                  maxLength={4000}
                  value={clarifications}
                  disabled={implementing}
                  onChange={(event) => setClarifications(event.target.value)}
                  placeholder="e.g. Use British spelling for user-facing copy; the config flag should default to false."
                />
              </label>
              <button
                className="primary-button"
                disabled={implementing}
                onClick={() => void handleImplement()}
                type="button"
              >
                {implementing
                  ? "Starting implementation..."
                  : "Approve and Implement"}
              </button>
            </div>
          ) : null}
          {!canImplement && job.state === "PLAN_READY" ? (
            <p className="meta-muted">
              Implementation approval is available only for local repository
              jobs.
            </p>
          ) : null}
          {job.state === "IMPLEMENTATION_QUEUED" ||
          job.state === "IMPLEMENTING" ||
          job.state === "VALIDATING" ? (
            <p className="banner banner-info">
              {JOB_STATE_LABELS[job.state]} in progress. This page will keep
              polling automatically.
            </p>
          ) : null}
        </section>
      ) : null}

      {job && implementationResult ? (
        <section className="plan-layout">
          <div className="panel">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Implementation result</span>
                <h2>Applied changes</h2>
              </div>
              <span className="pill success-pill">Ready</span>
            </div>
            <p>{implementationResult.summary}</p>
            {job.implementation_clarifications ? (
              <div className="field">
                <span className="meta-label">
                  Guidance considered during implementation
                </span>
                <p className="output-block">
                  {job.implementation_clarifications}
                </p>
              </div>
            ) : null}
          </div>

          <div className="plan-columns">
            <section className="panel">
              <h3>Changed files</h3>
              <ul className="content-list">
                {(implementationResult.changed_files ?? []).map((change) => (
                  <li
                    key={`${change.path}-${change.action}-${change.rationale}`}
                  >
                    <div className="change-header">
                      <code>{change.path}</code>
                      <span className="pill cap">{change.action}</span>
                    </div>
                    <p>{change.rationale}</p>
                  </li>
                ))}
              </ul>
            </section>

            <section className="panel">
              <h3>Diff</h3>
              {job.implementation_diff ? (
                <>
                  <p className="meta-muted">
                    This patch is captured from the isolated workspace after
                    implementation.
                  </p>
                  <details>
                    <summary className="pill-button">Show full patch</summary>
                    <pre className="output-block">
                      {job.implementation_diff.overall_patch}
                    </pre>
                  </details>
                  <ul className="content-list">
                    {(job.implementation_diff.files ?? []).map((file) => (
                      <li key={`${file.path}-${file.is_binary}`}>
                        <div className="change-header">
                          <code>{file.path}</code>
                          <span className="pill">{diffStat(file)}</span>
                        </div>
                        {file.is_binary ? (
                          <p className="meta-muted">
                            Binary file diff is not shown.
                          </p>
                        ) : (
                          <pre className="output-block">{file.patch}</pre>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="meta-muted">
                  No diff artifacts were recorded for this job.
                </p>
              )}
            </section>

            <section className="panel">
              <h3>Validation results</h3>
              <ul className="content-list">
                {(job.validation_results ?? []).map((result) => (
                  <li key={`${result.name}-${result.command}`}>
                    <div className="change-header">
                      <strong>{result.name}</strong>
                      <span
                        className={`pill validation-pill ${statusClass(result)}`}
                      >
                        {result.status.toLowerCase()}
                      </span>
                    </div>
                    <p>{result.summary}</p>
                    {result.command ? (
                      <code className="break-all">{result.command}</code>
                    ) : null}
                    {result.output_excerpt ? (
                      <pre className="output-block">
                        {result.output_excerpt}
                      </pre>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          </div>

          <div className="plan-columns">
            <section className="panel">
              <h3>Warnings</h3>
              {(implementationResult.warnings ?? []).length > 0 ? (
                <ul className="bullet-list">
                  {(implementationResult.warnings ?? []).map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              ) : (
                <p className="meta-muted">No warnings were reported.</p>
              )}
            </section>

            <section className="panel">
              <h3>Follow-up questions</h3>
              {(implementationResult.follow_up_questions ?? []).length > 0 ? (
                <ul className="bullet-list">
                  {(implementationResult.follow_up_questions ?? []).map(
                    (question) => (
                      <li key={question}>{question}</li>
                    ),
                  )}
                </ul>
              ) : (
                <p className="meta-muted">
                  No follow-up questions were recorded.
                </p>
              )}
            </section>
          </div>
        </section>
      ) : null}

      {!job && !error ? (
        <section className="panel">
          <p>Loading job details...</p>
        </section>
      ) : null}
    </div>
  );
}

function statusClass(result: ValidationResult): string {
  switch (result.status) {
    case "PASSED":
      return "is-passed";
    case "FAILED":
      return "is-failed";
    default:
      return "is-skipped";
  }
}
