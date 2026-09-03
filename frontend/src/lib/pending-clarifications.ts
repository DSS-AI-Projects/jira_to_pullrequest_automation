/**
 * Carries implementation clarifications from a failed job's Retry action
 * forward to the *new* job's implement-approval panel, once it exists.
 *
 * Unlike planning notes (see retry-draft.ts), clarifications have no field
 * on the job-creation form — they're only entered later, when approving
 * implementation on a PLAN_READY job. So the value can't be prefilled on
 * the form; instead the job-creation flow stashes it in sessionStorage
 * keyed by the *new* job's id (known right after `POST /jobs` returns),
 * and the new job's status page consumes it once, on first mount.
 */

const PENDING_KEY_PREFIX = "jira2pullreq:pending-clarifications:";

export function savePendingClarifications(jobId: string, text: string): void {
  if (!text) {
    return;
  }
  try {
    sessionStorage.setItem(PENDING_KEY_PREFIX + jobId, text);
  } catch {
    // Prefill is a convenience, not essential — fail silently if
    // sessionStorage is unavailable (private browsing, etc.).
  }
}

export function consumePendingClarifications(jobId: string): string | null {
  try {
    const key = PENDING_KEY_PREFIX + jobId;
    const raw = sessionStorage.getItem(key);
    if (!raw) {
      return null;
    }
    sessionStorage.removeItem(key);
    return raw;
  } catch {
    return null;
  }
}
