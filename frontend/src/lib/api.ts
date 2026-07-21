import type { Plan } from "@/lib/plan.gen";

export type JobState =
  | "QUEUED"
  | "FETCHING_TICKET"
  | "CLONING_REPO"
  | "MAPPING_REPO"
  | "PLANNING"
  | "PLAN_READY"
  | "IMPLEMENTATION_QUEUED"
  | "IMPLEMENTING"
  | "VALIDATING"
  | "IMPLEMENTATION_READY"
  | "IMPLEMENTATION_FAILED"
  | "FAILED";

export type ApiErrorPayload = {
  error: {
    code: string;
    message: string;
  };
};

export type CurrentUser = {
  id: string;
  email: string;
  display_name: string;
  role: "USER" | "ADMIN";
};

export type SessionInfo = {
  auth_enabled: boolean;
  can_dev_login: boolean;
  user: CurrentUser | null;
};

export type JobError = {
  code: string;
  message: string;
  stage: JobState;
};

export type RepoSourceKind = "REMOTE" | "LOCAL";

export type RepoInfo = {
  source_kind: RepoSourceKind;
  branch: string;
  commit_sha: string;
  origin_url: string | null;
  is_dirty: boolean;
  local_path: string | null;
};

export type AgentUsage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_cost_usd: number | null;
  num_turns: number | null;
  duration_seconds: number;
};

export type ImplementationChange = {
  path: string;
  action: string;
  rationale: string;
};

export type ImplementationResult = {
  summary: string;
  changed_files: ImplementationChange[];
  warnings: string[];
  follow_up_questions: string[];
};

export type ValidationStatus = "PASSED" | "FAILED" | "SKIPPED";

export type ValidationResult = {
  name: string;
  command: string;
  status: ValidationStatus;
  summary: string;
  output_excerpt: string | null;
};

export type Job = {
  id: string;
  ticket_key: string;
  repo_url: string;
  state: JobState;
  error: JobError | null;
  repo_info: RepoInfo | null;
  workspace_path: string | null;
  plan: Plan | null;
  usage: AgentUsage | null;
  implementation_usage: AgentUsage | null;
  implementation_result: ImplementationResult | null;
  validation_results: ValidationResult[];
  implementation_approved_at: string | null;
  implementation_started_at: string | null;
  implementation_finished_at: string | null;
  created_at: string;
  updated_at: string;
};

export type RepoChoice = {
  name: string;
  url: string;
};

export type RepoList = {
  repos: RepoChoice[];
  allowed_hosts: string[];
  local_repo_support: {
    enabled: boolean;
    allowed_roots: string[];
    allow_dirty: boolean;
    require_ticket_branch_match: boolean;
  };
};

export type JobCreated = {
  job_id: string;
};

type LocationLike = {
  origin: string;
  protocol: string;
  hostname: string;
  port: string;
};

const LOCAL_DEV_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const LOCAL_DEV_FRONTEND_PORTS = new Set(["3000", "3001", "3010"]);

export function defaultApiBaseUrl(locationLike?: LocationLike | null): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "");
  if (configured) {
    return configured;
  }

  const currentLocation =
    locationLike ?? (typeof window !== "undefined" ? window.location : null);
  if (currentLocation) {
    if (
      LOCAL_DEV_HOSTS.has(currentLocation.hostname) &&
      LOCAL_DEV_FRONTEND_PORTS.has(currentLocation.port)
    ) {
      return `${currentLocation.protocol}//${currentLocation.hostname}:8000`;
    }
    return currentLocation.origin.replace(/\/$/, "");
  }
  return "http://127.0.0.1:8000";
}

export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "AbortError") {
    return true;
  }
  if (error instanceof Error) {
    return (
      error.name === "AbortError" ||
      error.message.toLowerCase().includes("aborted")
    );
  }
  return false;
}

async function parseJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T | ApiErrorPayload;
  if (!response.ok) {
    const error = body as ApiErrorPayload;
    throw new Error(
      error.error?.message ?? `Request failed with status ${response.status}`,
    );
  }
  return body as T;
}

async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  return fetch(`${defaultApiBaseUrl()}${path}`, {
    credentials: "include",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
}

function normalizeJob(job: Job): Job {
  return {
    ...job,
    validation_results: Array.isArray(job.validation_results)
      ? job.validation_results
      : [],
    implementation_usage: job.implementation_usage ?? null,
    implementation_result: job.implementation_result
      ? {
          ...job.implementation_result,
          changed_files: Array.isArray(job.implementation_result.changed_files)
            ? job.implementation_result.changed_files
            : [],
          warnings: Array.isArray(job.implementation_result.warnings)
            ? job.implementation_result.warnings
            : [],
          follow_up_questions: Array.isArray(
            job.implementation_result.follow_up_questions,
          )
            ? job.implementation_result.follow_up_questions
            : [],
        }
      : null,
  };
}

export async function fetchRepos(signal?: AbortSignal): Promise<RepoList> {
  const response = await apiFetch("/api/repos", {
    signal,
  });
  return parseJson<RepoList>(response);
}

export async function createJob(
  payload: {
    ticket: string;
    repo: string;
  },
  signal?: AbortSignal,
): Promise<JobCreated> {
  const response = await apiFetch("/api/jobs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });
  return parseJson<JobCreated>(response);
}

export async function fetchJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<Job> {
  const response = await apiFetch(`/api/jobs/${jobId}`, {
    signal,
    cache: "no-store",
  });
  return normalizeJob(await parseJson<Job>(response));
}

export async function implementJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobCreated> {
  const response = await apiFetch(`/api/jobs/${jobId}/implement`, {
    method: "POST",
    signal,
  });
  return parseJson<JobCreated>(response);
}

export async function fetchSession(signal?: AbortSignal): Promise<SessionInfo> {
  const response = await apiFetch("/api/auth/session", {
    signal,
    cache: "no-store",
  });
  return parseJson<SessionInfo>(response);
}

export async function devLogin(
  payload: { email: string; display_name: string },
  signal?: AbortSignal,
): Promise<SessionInfo> {
  const response = await apiFetch("/api/auth/dev-login", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });
  return parseJson<SessionInfo>(response);
}

export async function logout(signal?: AbortSignal): Promise<void> {
  const response = await apiFetch("/api/auth/logout", {
    method: "POST",
    signal,
  });
  await parseJson<{ ok: boolean }>(response);
}
