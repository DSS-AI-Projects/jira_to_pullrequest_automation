/**
 * Carries a failed job's original inputs from the detail page's Retry
 * button to the job-creation form. sessionStorage (not URL query params) so
 * planning_notes — up to 4000 chars — never has to round-trip through a URL.
 */

export type RetryDraft = {
  ticket: string;
  repo: string;
  repoMode: "remote" | "local";
  // The branch the failed job was cloned from, if any — "" means "use the
  // repo's default branch" (today's unchanged behavior). Defensive fallback
  // for a draft saved by an older build predates this field, so
  // consumeRetryDraft's caller must still handle it being undefined.
  baseBranch: string;
  planningNotes: string;
  // A document upload can't be carried forward (files aren't persisted in
  // sessionStorage) — requirementDocumentName is shown so the user knows
  // what to re-upload; the file input itself always starts empty.
  requirementSource: "JIRA" | "DOCUMENT";
  requirementDocumentName: string | null;
  // The failed job's resolved ticket key, carried forward only when it's a
  // real Jira key (typed or auto-detected — see routes.py's
  // _resolve_document_ticket_key) rather than the opaque DOC-XXXXXXXX
  // fallback, which would mean nothing typed back into the form. Empty
  // string means "nothing to prefill" — same convention as the other
  // optional fields on this draft.
  documentTicketKey: string;
  // The failed job's implementation clarifications, if any. There's no
  // field for this on the creation form — job-form.tsx forwards it to
  // pending-clarifications.ts once the new job's id is known, so it can
  // prefill that job's own implement-approval panel later.
  implementationClarifications: string;
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
