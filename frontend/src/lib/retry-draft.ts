/**
 * Carries a failed job's original inputs from the detail page's Retry
 * button to the job-creation form. sessionStorage (not URL query params) so
 * planning_notes — up to 4000 chars — never has to round-trip through a URL.
 */

export type RetryDraft = {
  ticket: string;
  repo: string;
  repoMode: "remote" | "local";
  planningNotes: string;
};

const STORAGE_KEY = "jira2pullreq:retry-draft";

export function saveRetryDraft(draft: RetryDraft): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
  } catch {
    // Retry prefill is a convenience, not essential — fail silently if
    // sessionStorage is unavailable (private browsing, etc.).
  }
}

export function consumeRetryDraft(): RetryDraft | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    sessionStorage.removeItem(STORAGE_KEY);
    return JSON.parse(raw) as RetryDraft;
  } catch {
    return null;
  }
}
