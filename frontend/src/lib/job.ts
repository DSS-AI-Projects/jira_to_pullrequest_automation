import type { JobState } from "@/lib/api";

// CORRECTING/REVALIDATING are deliberately not included here: they're a
// post-hoc "fix the validation failures" addendum a developer can trigger
// after reaching IMPLEMENTATION_READY, not a continuation of the linear
// plan -> implement pipeline this timeline stepper represents. See the
// correction status shown separately in job-status-view.tsx.
export const JOB_STATES: JobState[] = [
  "QUEUED",
  "FETCHING_TICKET",
  "CLONING_REPO",
  "MAPPING_REPO",
  "PLANNING",
  "PLAN_READY",
  "IMPLEMENTATION_QUEUED",
  "IMPLEMENTING",
  "VALIDATING",
  "IMPLEMENTATION_READY",
];

export const JOB_STATE_LABELS: Record<JobState, string> = {
  QUEUED: "Queued",
  FETCHING_TICKET: "Fetching ticket",
  CLONING_REPO: "Cloning repository",
  MAPPING_REPO: "Mapping repository",
  PLANNING: "Generating plan",
  PLAN_READY: "Plan ready",
  IMPLEMENTATION_QUEUED: "Implementation queued",
  IMPLEMENTING: "Applying changes",
  VALIDATING: "Running validation",
  CORRECTING: "Attempting automatic fix",
  REVALIDATING: "Re-running validation",
  IMPLEMENTATION_READY: "Implementation ready",
  IMPLEMENTATION_FAILED: "Implementation failed",
  FAILED: "Failed",
};

export function isTerminalState(state: JobState): boolean {
  return (
    state === "PLAN_READY" ||
    state === "IMPLEMENTATION_READY" ||
    state === "IMPLEMENTATION_FAILED" ||
    state === "FAILED"
  );
}
