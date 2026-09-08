"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  correctValidation,
  createBranch,
  fetchJob,
  implementJob,
  isAbortError,
  pushBranch,
  type Job,
  type ImplementationDiffFile,
  type ValidationResult,
} from "@/lib/api";
import { isTerminalState, JOB_STATES, JOB_STATE_LABELS } from "@/lib/job";
import { consumePendingClarifications } from "@/lib/pending-clarifications";
import { saveRetryDraft } from "@/lib/retry-draft";

import { PlanView } from "./plan-view";

const POLL_INTERVAL_MS = 2000;

function isLocalSource(job: Job): boolean {
  return (
    job.repo_info?.source_kind === "LOCAL" ||
    job.repo_info?.source_kind === "LOCAL_FOLDER"
  );
}

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

// Mirrors github_compare_url() in backend/app/api/routes.py — a plain web
// link, not an API call, so it never opens or creates a PR itself.
const GITHUB_HTTPS_RE =
  /^https:\/\/github\.com\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/;
const GITHUB_SSH_RE =
  /^(?:ssh:\/\/)?git@github\.com[:/]([^/]+)\/([^/]+?)(?:\.git)?\/?$/;

function githubCompareUrl(remoteUrl: string, branch: string): string | null {
  const match =
    GITHUB_HTTPS_RE.exec(remoteUrl) ?? GITHUB_SSH_RE.exec(remoteUrl);
  if (!match) {
    return null;
  }
  const [, owner, repo] = match;
  return `https://github.com/${owner}/${repo}/compare/${branch}?expand=1`;
}

export function JobStatusView(props: { jobId: string }) {
  const router = useRouter();
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [implementing, setImplementing] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const [clarifications, setClarifications] = useState("");
  const [branchNameInput, setBranchNameInput] = useState("");
  const [commitMessageInput, setCommitMessageInput] = useState("");
  const [creatingBranch, setCreatingBranch] = useState(false);
  const [pushBranchNameInput, setPushBranchNameInput] = useState("");
  const [pushingBranch, setPushingBranch] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "error">(
    "idle",
  );

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
    const pending = consumePendingClarifications(props.jobId);
    if (pending) {
      setClarifications(pending);
    }
  }, [props.jobId]);

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

  // activity_log is reset at the start of each agent-backed step (see
  // agent_progress.py) and then holds that step's log until the next one
  // starts, so a FAILED job still shows exactly what the agent was doing
  // right up to the failure — but the title should only read present-tense
  // ("Applying code changes") while actually running; once terminal, a
  // generic past-tense title avoids implying it's still in progress.
  const isAgentActive =
    job?.state === "PLANNING" ||
    job?.state === "IMPLEMENTING" ||
    job?.state === "CORRECTING";
  const activityLog = job?.activity_log ?? [];
  const activityLogTitle =
    job?.state === "PLANNING"
      ? "Generating the plan"
      : job?.state === "IMPLEMENTING"
        ? "Applying code changes"
        : job?.state === "CORRECTING"
          ? "Attempting automatic fix"
          : "Last agent activity";

  // Implementation runs entirely in the isolated workspace clone regardless
  // of source kind, so it's available for every job that has reached
  // PLAN_READY — remote-sourced jobs included.
  const canImplement = job?.state === "PLAN_READY";

  const implementationResult =
    job?.state === "IMPLEMENTATION_READY" ? job.implementation_result : null;

  const canRetry =
    job?.state === "FAILED" || job?.state === "IMPLEMENTATION_FAILED";

  // Excludes a failed "npm install" step: it's a missing-dependency /
  // environment problem, not a code defect, so no source edit could ever
  // fix it — mirrors correctable_failures() in the backend's
  // validation_runner.py.
  const hasCorrectableFailure = job
    ? (job.validation_results ?? []).some(
        (result) => result.status === "FAILED" && result.name !== "npm install",
      )
    : false;
  const correctionInProgress =
    job?.state === "CORRECTING" || job?.state === "REVALIDATING";
  const canCorrectValidation =
    job?.state === "IMPLEMENTATION_READY" &&
    hasCorrectableFailure &&
    !job.implementation_correction_attempted;

  const canCreateBranch =
    job?.state === "IMPLEMENTATION_READY" && !job.branch_name;

  const canPushBranch = !!job?.branch_name && !job.branch_pushed_at;

  function handleRetry() {
    if (!job) {
      return;
    }
    saveRetryDraft({
      // A DOCUMENT job's ticket_key is a synthetic "DOC-..." id, not a real
      // Jira ticket — never prefill it as if it were one.
      ticket: job.requirement_source === "DOCUMENT" ? "" : job.ticket_key,
      repo: job.repo_url,
      repoMode: isLocalSource(job) ? "local" : "remote",
      planningNotes: job.planning_notes ?? "",
      requirementSource: job.requirement_source,
      requirementDocumentName: job.requirement_document_name,
      implementationClarifications: job.implementation_clarifications ?? "",
    });
    router.push("/");
  }

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

  async function handleCorrectValidation() {
    if (!job) {
      return;
    }
    setCorrecting(true);
    setError(null);
    try {
      await correctValidation(job.id);
      setRefreshKey((value) => value + 1);
    } catch (correctError) {
      setError(
        correctError instanceof Error
          ? correctError.message
          : "Could not start validation correction.",
      );
    } finally {
      setCorrecting(false);
    }
  }

  async function handleCreateBranch() {
    if (!job) {
      return;
    }
    setCreatingBranch(true);
    setError(null);
    try {
      await createBranch(job.id, {
        branch_name: branchNameInput,
        commit_message: commitMessageInput,
      });
      setRefreshKey((value) => value + 1);
    } catch (branchError) {
      setError(
        branchError instanceof Error
          ? branchError.message
          : "Could not create the branch.",
      );
    } finally {
      setCreatingBranch(false);
    }
  }

  async function handlePushBranch() {
    if (!job) {
      return;
    }
    setPushingBranch(true);
    setError(null);
    try {
      await pushBranch(job.id, { branch_name: pushBranchNameInput });
      setRefreshKey((value) => value + 1);
    } catch (pushError) {
      setError(
        pushError instanceof Error
          ? pushError.message
          : "Could not push the branch.",
      );
    } finally {
      setPushingBranch(false);
    }
  }

  async function handleCopyPatch(patch: string) {
    try {
      await navigator.clipboard.writeText(patch);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("error");
    } finally {
      setTimeout(() => setCopyStatus("idle"), 2000);
    }
  }

  function handleDownloadPatch(
    patch: string,
    jobId: string,
    ticketKey: string,
  ) {
    const blob = new Blob([patch], { type: "text/x-diff" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${ticketKey}-${jobId.slice(0, 8)}.patch`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="job-layout">
      <div className="status-row">
        <section className="panel status-header">
          <div className="status-heading">
            <div>
              <span className="eyebrow">Job status</span>
              <h1>Planning job {props.jobId}</h1>
              <p>
                Review the generated plan, approve implementation when ready,
                and track implementation plus validation through to completion.
              </p>
            </div>
            <div className="actions">
              {canRetry ? (
                <button
                  className="secondary-link"
                  onClick={handleRetry}
                  type="button"
                >
                  Retry
                </button>
              ) : null}
              <Link className="secondary-link" href="/">
                New job
              </Link>
              <Link className="secondary-link" href="/jobs">
                All jobs
              </Link>
            </div>
          </div>

          {error ? <p className="banner banner-error">{error}</p> : null}

          <ol className="status-timeline">
            {JOB_STATES.map((state, index) => {
              const jobFailed =
                job?.state === "FAILED" ||
                job?.state === "IMPLEMENTATION_FAILED";
              const isFailed = jobFailed && job?.error?.stage === state;
              const isActive = index === activeIndex && !isFailed;
              const isComplete = job
                ? index < activeIndex || job.state === "IMPLEMENTATION_READY"
                : false;
              return (
                <li
                  className={[
                    "timeline-item",
                    isActive ? "is-active" : "",
                    isComplete ? "is-complete" : "",
                    isFailed ? "is-failed" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  key={state}
                >
                  <span className="timeline-badge">{index + 1}</span>
                  <div>
                    <strong>{JOB_STATE_LABELS[state]}</strong>
                    {job?.state === state ? <p>Current step</p> : null}
                    {isFailed ? (
                      <p className="timeline-failed-label">Failed here</p>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>

        {job && (activityLog.length > 0 || isAgentActive) ? (
          <section className="panel activity-log-panel">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Agent activity</span>
                <h2>{activityLogTitle}</h2>
              </div>
            </div>
            {activityLog.length > 0 ? (
              <ul className="activity-log-list">
                {[...activityLog].reverse().map((line, index) => (
                  <li key={`${activityLog.length - index}-${line}`}>{line}</li>
                ))}
              </ul>
            ) : (
              <p className="meta-muted">Waiting for activity…</p>
            )}
          </section>
        ) : null}
      </div>

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
            <strong
              className={
                job.state === "FAILED" || job.state === "IMPLEMENTATION_FAILED"
                  ? "state-failed"
                  : undefined
              }
            >
              {JOB_STATE_LABELS[job.state]}
            </strong>
          </div>
          {job.repo_info ? (
            <>
              <div className="summary-card">
                <span className="meta-label">Source</span>
                <strong>
                  {job.repo_info.source_kind === "LOCAL"
                    ? "Local repo"
                    : job.repo_info.source_kind === "LOCAL_FOLDER"
                      ? "Local folder (no git)"
                      : "Remote repo"}
                </strong>
              </div>
              <div className="summary-card">
                <span className="meta-label">Branch</span>
                <strong className="break-all">
                  {job.repo_info.branch ?? "Not applicable"}
                </strong>
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

      {job?.state === "IMPLEMENTATION_FAILED" && job.implementation_diff ? (
        <section className="panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Partial changes</span>
              <h2>Changes made before the failure</h2>
              <p className="meta-muted">
                The agent had already written these changes to the isolated
                workspace before the job failed. They were never validated or
                summarized by the agent, so review carefully before reusing
                them.
              </p>
            </div>
          </div>
          <div className="actions">
            <button
              className="pill-button"
              onClick={() =>
                void handleCopyPatch(
                  job.implementation_diff?.overall_patch ?? "",
                )
              }
              type="button"
            >
              {copyStatus === "copied"
                ? "Copied!"
                : copyStatus === "error"
                  ? "Copy failed"
                  : "Copy patch"}
            </button>
            <button
              className="pill-button"
              onClick={() =>
                handleDownloadPatch(
                  job.implementation_diff?.overall_patch ?? "",
                  job.id,
                  job.ticket_key,
                )
              }
              type="button"
            >
              Download patch
            </button>
          </div>
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
                  <p className="meta-muted">Binary file diff is not shown.</p>
                ) : (
                  <pre className="output-block">{file.patch}</pre>
                )}
              </li>
            ))}
          </ul>
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
            <p className="preserve-whitespace">
              {implementationResult.summary}
            </p>
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
                  <div className="actions">
                    <button
                      className="pill-button"
                      onClick={() =>
                        void handleCopyPatch(
                          job.implementation_diff?.overall_patch ?? "",
                        )
                      }
                      type="button"
                    >
                      {copyStatus === "copied"
                        ? "Copied!"
                        : copyStatus === "error"
                          ? "Copy failed"
                          : "Copy patch"}
                    </button>
                    <button
                      className="pill-button"
                      onClick={() =>
                        handleDownloadPatch(
                          job.implementation_diff?.overall_patch ?? "",
                          job.id,
                          job.ticket_key,
                        )
                      }
                      type="button"
                    >
                      Download patch
                    </button>
                  </div>
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

      {job &&
      (hasCorrectableFailure || job.implementation_correction_attempted) ? (
        <section className="panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Validation correction</span>
              <h2>Attempt automatic fix</h2>
              <p>
                One or more validation checks failed. You can ask the agent to
                attempt a targeted fix in the same workspace — limited to one
                attempt per job.
              </p>
            </div>
          </div>
          {canCorrectValidation ? (
            <div className="actions">
              <button
                className="primary-button"
                disabled={correcting}
                onClick={() => void handleCorrectValidation()}
                type="button"
              >
                {correcting
                  ? "Starting correction..."
                  : "Attempt automatic fix"}
              </button>
            </div>
          ) : null}
          {correctionInProgress ? (
            <p className="banner banner-info">
              {JOB_STATE_LABELS[job.state]} in progress. This page will keep
              polling automatically.
            </p>
          ) : null}
          {job.implementation_correction_attempted ? (
            <div className="stack">
              {job.implementation_correction_error ? (
                <p className="banner banner-error">
                  {job.implementation_correction_error.message}
                </p>
              ) : job.implementation_correction_result ? (
                <>
                  <p className="preserve-whitespace">
                    {job.implementation_correction_result.summary}
                  </p>
                  {(job.implementation_correction_result.changed_files ?? [])
                    .length > 0 ? (
                    <ul className="content-list">
                      {job.implementation_correction_result.changed_files.map(
                        (change) => (
                          <li
                            key={`${change.path}-${change.action}-${change.rationale}`}
                          >
                            <div className="change-header">
                              <code>{change.path}</code>
                              <span className="pill cap">{change.action}</span>
                            </div>
                            <p>{change.rationale}</p>
                          </li>
                        ),
                      )}
                    </ul>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      {job && (canCreateBranch || job.branch_name) ? (
        <section className="panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Branch preparation</span>
              <h2>Create branch</h2>
              <p>
                Commit the reviewed diff to a new branch inside the isolated
                workspace. This never pushes anywhere or touches your original
                repository.
              </p>
            </div>
          </div>
          {canCreateBranch ? (
            <div className="stack">
              <label className="field">
                <span>Branch name (optional)</span>
                <small>
                  Leave blank to use{" "}
                  {job.requirement_source === "DOCUMENT"
                    ? "a name based on the plan summary"
                    : `"jira2pullreq/${job.ticket_key}"`}
                  .
                </small>
                <input
                  className="text-input"
                  disabled={creatingBranch}
                  maxLength={200}
                  onChange={(event) => setBranchNameInput(event.target.value)}
                  placeholder={`jira2pullreq/${job.ticket_key}`}
                  type="text"
                  value={branchNameInput}
                />
              </label>
              <label className="field">
                <span>Commit message (optional)</span>
                <small>
                  Leave blank to use &quot;{job.ticket_key}:{" "}
                  {job.plan?.summary ?? "..."}&quot;.
                </small>
                <textarea
                  className="text-input"
                  disabled={creatingBranch}
                  maxLength={2000}
                  onChange={(event) =>
                    setCommitMessageInput(event.target.value)
                  }
                  placeholder={`${job.ticket_key}: ${job.plan?.summary ?? ""}`}
                  rows={2}
                  value={commitMessageInput}
                />
              </label>
              <div className="actions">
                <button
                  className="primary-button"
                  disabled={creatingBranch}
                  onClick={() => void handleCreateBranch()}
                  type="button"
                >
                  {creatingBranch ? "Creating branch..." : "Create branch"}
                </button>
              </div>
            </div>
          ) : null}
          {job.branch_name ? (
            <div className="stack">
              <p>
                Created branch <code>{job.branch_name}</code> at commit{" "}
                <code>{job.branch_commit_sha?.slice(0, 12)}</code>.
              </p>
            </div>
          ) : null}
        </section>
      ) : null}

      {job?.branch_name ? (
        <section className="panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Branch preparation</span>
              <h2>Push branch</h2>
              <p>
                Push the created branch to this repository&apos;s remote. This
                never force-pushes or overwrites an existing branch.
              </p>
            </div>
          </div>
          {canPushBranch ? (
            <div className="stack">
              <label className="field">
                <span>Branch name (optional)</span>
                <small>
                  Leave blank to push as <code>{job.branch_name}</code>. Only
                  needed to rename before pushing, e.g. after a name collision
                  on the remote.
                </small>
                <input
                  className="text-input"
                  disabled={pushingBranch}
                  maxLength={200}
                  onChange={(event) =>
                    setPushBranchNameInput(event.target.value)
                  }
                  placeholder={job.branch_name}
                  type="text"
                  value={pushBranchNameInput}
                />
              </label>
              <div className="actions">
                <button
                  className="primary-button"
                  disabled={pushingBranch}
                  onClick={() => void handlePushBranch()}
                  type="button"
                >
                  {pushingBranch ? "Pushing..." : "Push branch"}
                </button>
              </div>
            </div>
          ) : null}
          {job.branch_pushed_at ? (
            <div className="stack">
              <p>
                Pushed <code>{job.branch_name}</code> to{" "}
                <span className="break-all">{job.branch_push_remote_url}</span>.
              </p>
              {job.branch_push_remote_url &&
              githubCompareUrl(job.branch_push_remote_url, job.branch_name) ? (
                <Link
                  className="secondary-link"
                  href={githubCompareUrl(
                    job.branch_push_remote_url,
                    job.branch_name,
                  )!}
                >
                  Open a pull request on GitHub
                </Link>
              ) : null}
            </div>
          ) : null}
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
