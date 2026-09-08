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
  | "CORRECTING"
  | "REVALIDATING"
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

export type JiraAuthMode = "UNCONFIGURED" | "SHARED" | "DELEGATED";

export type JiraCloudSite = {
  id: string;
  name: string;
  url: string;
};

export type JiraConnectionInfo = {
  site: JiraCloudSite;
  scopes: string[];
  connected_at: string;
  updated_at: string;
  access_token_expires_at: string;
  has_refresh_token: boolean;
};

export type JiraAuthStatus = {
  oauth_enabled: boolean;
  oauth_configured: boolean;
  shared_configured: boolean;
  effective_mode: JiraAuthMode;
  connected: boolean;
  connection: JiraConnectionInfo | null;
};

export type JiraConnectStartResponse = {
  authorization_url: string;
};

export type JiraConnectCallbackResponse = {
  ok: boolean;
  connection: JiraConnectionInfo;
};

export type RepoHostingProvider = "GITHUB" | "GITLAB";

export type RepoHostingAuthKind = "OAUTH_USER" | "APP_INSTALLATION";

export type RepoHostingConnectionInfo = {
  provider: RepoHostingProvider;
  auth_kind: RepoHostingAuthKind;
  account_name: string;
  account_id: string;
  account_url: string;
  scopes: string[];
  installation_id: string | null;
  connected_at: string;
  updated_at: string;
  access_token_expires_at: string | null;
  has_refresh_token: boolean;
};

export type RepoHostingProviderStatus = {
  provider: RepoHostingProvider;
  display_name: string;
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  connection: RepoHostingConnectionInfo | null;
};

export type RepoHostingStatus = {
  providers: RepoHostingProviderStatus[];
};

export type RepoHostingConnectStartResponse = {
  authorization_url: string;
};

export type RepoHostingConnectCallbackResponse = {
  ok: boolean;
  connection: RepoHostingConnectionInfo;
};

export type GitHubRepositorySummary = {
  id: number;
  name: string;
  full_name: string;
  html_url: string;
  clone_url: string;
  default_branch: string | null;
  owner_login: string;
  private: boolean;
};

export type GitHubRepositoryListResponse = {
  repos: GitHubRepositorySummary[];
};

export type JobError = {
  code: string;
  message: string;
  stage: JobState;
};

export type RepoSourceKind = "REMOTE" | "LOCAL" | "LOCAL_FOLDER";

export type RepoInfo = {
  source_kind: RepoSourceKind;
  // null for LOCAL_FOLDER: a plain folder has no git branch to report.
  branch: string | null;
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

export type ImplementationDiffFile = {
  path: string;
  patch: string;
  additions: number | null;
  deletions: number | null;
  is_binary: boolean;
};

export type ImplementationDiff = {
  overall_patch: string;
  files: ImplementationDiffFile[];
};

export type ValidationStatus = "PASSED" | "FAILED" | "SKIPPED";

export type ValidationResult = {
  name: string;
  command: string;
  status: ValidationStatus;
  summary: string;
  output_excerpt: string | null;
};

export type RequirementSource = "JIRA" | "DOCUMENT";

export type Job = {
  id: string;
  ticket_key: string;
  requirement_source: RequirementSource;
  requirement_document_name: string | null;
  repo_url: string;
  planning_notes: string | null;
  state: JobState;
  error: JobError | null;
  repo_info: RepoInfo | null;
  workspace_path: string | null;
  plan: Plan | null;
  usage: AgentUsage | null;
  implementation_usage: AgentUsage | null;
  implementation_result: ImplementationResult | null;
  implementation_diff: ImplementationDiff | null;
  validation_results: ValidationResult[];
  implementation_clarifications: string | null;
  implementation_approved_at: string | null;
  implementation_started_at: string | null;
  implementation_finished_at: string | null;
  implementation_baseline_commit_sha: string | null;
  implementation_correction_attempted: boolean;
  implementation_correction_result: ImplementationResult | null;
  implementation_correction_error: JobError | null;
  branch_name: string | null;
  branch_commit_sha: string | null;
  branch_created_at: string | null;
  branch_pushed_at: string | null;
  branch_push_remote_url: string | null;
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
    allow_non_git_folders: boolean;
  };
};

export type JobCreated = {
  job_id: string;
};

export type JobSummary = {
  id: string;
  ticket_key: string;
  repo_url: string;
  state: JobState;
  created_at: string;
  updated_at: string;
  error_code: string | null;
};

export type JobListResponse = {
  jobs: JobSummary[];
  next_cursor: string | null;
};

export type OwnerCostSummary = {
  user_id: string | null;
  email: string | null;
  display_name: string | null;
  job_count: number;
  planning_cost_usd: number;
  implementation_cost_usd: number;
  total_cost_usd: number;
};

export type CostSummaryResponse = {
  owners: OwnerCostSummary[];
  grand_total_usd: number;
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

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function parseJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T | ApiErrorPayload;
  if (!response.ok) {
    const error = body as ApiErrorPayload;
    throw new ApiError(
      error.error?.message ?? `Request failed with status ${response.status}`,
      error.error?.code ?? "UNKNOWN",
      response.status,
    );
  }
  return body as T;
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
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
    implementation_diff: job.implementation_diff
      ? {
          ...job.implementation_diff,
          files: Array.isArray(job.implementation_diff.files)
            ? job.implementation_diff.files.map((file) => ({
                ...file,
                additions: file.additions ?? null,
                deletions: file.deletions ?? null,
                is_binary: file.is_binary ?? false,
              }))
            : [],
        }
      : null,
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
    implementation_correction_result: job.implementation_correction_result
      ? {
          ...job.implementation_correction_result,
          changed_files: Array.isArray(
            job.implementation_correction_result.changed_files,
          )
            ? job.implementation_correction_result.changed_files
            : [],
          warnings: Array.isArray(job.implementation_correction_result.warnings)
            ? job.implementation_correction_result.warnings
            : [],
          follow_up_questions: Array.isArray(
            job.implementation_correction_result.follow_up_questions,
          )
            ? job.implementation_correction_result.follow_up_questions
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
    ticket?: string;
    repo: string;
    planning_notes?: string;
    requirement_document?: File;
  },
  signal?: AbortSignal,
): Promise<JobCreated> {
  // FormData, not JSON: a requirement document upload requires a multipart
  // body. Never set Content-Type manually here — the browser must supply
  // the multipart boundary itself.
  const formData = new FormData();
  if (payload.ticket !== undefined) {
    formData.append("ticket", payload.ticket);
  }
  formData.append("repo", payload.repo);
  if (payload.planning_notes !== undefined) {
    formData.append("planning_notes", payload.planning_notes);
  }
  if (payload.requirement_document) {
    formData.append("requirement_document", payload.requirement_document);
  }
  const response = await apiFetch("/api/jobs", {
    method: "POST",
    body: formData,
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

export async function fetchJobs(
  params?: { limit?: number; before?: string },
  signal?: AbortSignal,
): Promise<JobListResponse> {
  const search = new URLSearchParams();
  if (params?.limit) {
    search.set("limit", String(params.limit));
  }
  if (params?.before) {
    search.set("before", params.before);
  }
  const query = search.toString();
  const response = await apiFetch(`/api/jobs${query ? `?${query}` : ""}`, {
    signal,
    cache: "no-store",
  });
  return parseJson<JobListResponse>(response);
}

export async function fetchCostSummary(
  signal?: AbortSignal,
): Promise<CostSummaryResponse> {
  const response = await apiFetch("/api/admin/cost-summary", {
    signal,
    cache: "no-store",
  });
  return parseJson<CostSummaryResponse>(response);
}

export async function implementJob(
  jobId: string,
  payload?: { clarifications?: string },
  signal?: AbortSignal,
): Promise<JobCreated> {
  const clarifications = payload?.clarifications?.trim();
  const response = await apiFetch(`/api/jobs/${jobId}/implement`, {
    method: "POST",
    ...(clarifications
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ clarifications }),
        }
      : {}),
    signal,
  });
  return parseJson<JobCreated>(response);
}

export async function correctValidation(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobCreated> {
  const response = await apiFetch(`/api/jobs/${jobId}/correct-validation`, {
    method: "POST",
    signal,
  });
  return parseJson<JobCreated>(response);
}

export type BranchCreated = {
  branch_name: string;
  commit_sha: string;
};

export async function createBranch(
  jobId: string,
  payload?: { branch_name?: string; commit_message?: string },
  signal?: AbortSignal,
): Promise<BranchCreated> {
  const branchName = payload?.branch_name?.trim();
  const commitMessage = payload?.commit_message?.trim();
  const body = {
    ...(branchName ? { branch_name: branchName } : {}),
    ...(commitMessage ? { commit_message: commitMessage } : {}),
  };
  const response = await apiFetch(`/api/jobs/${jobId}/create-branch`, {
    method: "POST",
    ...(Object.keys(body).length > 0
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
    signal,
  });
  return parseJson<BranchCreated>(response);
}

export type BranchPushed = {
  branch_name: string;
  remote_url: string;
  compare_url: string | null;
};

export async function pushBranch(
  jobId: string,
  payload?: { branch_name?: string },
  signal?: AbortSignal,
): Promise<BranchPushed> {
  const branchName = payload?.branch_name?.trim();
  const body = branchName ? { branch_name: branchName } : {};
  const response = await apiFetch(`/api/jobs/${jobId}/push-branch`, {
    method: "POST",
    ...(Object.keys(body).length > 0
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
    signal,
  });
  return parseJson<BranchPushed>(response);
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

export async function fetchJiraAuthStatus(
  signal?: AbortSignal,
): Promise<JiraAuthStatus> {
  const response = await apiFetch("/api/auth/jira", {
    signal,
    cache: "no-store",
  });
  return parseJson<JiraAuthStatus>(response);
}

export async function startJiraConnect(
  signal?: AbortSignal,
): Promise<JiraConnectStartResponse> {
  const response = await apiFetch("/api/auth/jira/connect", {
    method: "POST",
    signal,
  });
  return parseJson<JiraConnectStartResponse>(response);
}

export async function completeJiraConnect(
  params: { code: string; state: string },
  signal?: AbortSignal,
): Promise<JiraConnectCallbackResponse> {
  const search = new URLSearchParams(params).toString();
  const response = await apiFetch(`/api/auth/jira/callback?${search}`, {
    signal,
    cache: "no-store",
  });
  return parseJson<JiraConnectCallbackResponse>(response);
}

export async function disconnectJira(signal?: AbortSignal): Promise<void> {
  const response = await apiFetch("/api/auth/jira", {
    method: "DELETE",
    signal,
  });
  await parseJson<{ ok: boolean }>(response);
}

export async function fetchRepoHostingStatus(
  signal?: AbortSignal,
): Promise<RepoHostingStatus> {
  const response = await apiFetch("/api/auth/repo-hosting", {
    signal,
    cache: "no-store",
  });
  return parseJson<RepoHostingStatus>(response);
}

export async function disconnectRepoHostingProvider(
  provider: RepoHostingProvider,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(`/api/auth/repo-hosting/${provider}`, {
    method: "DELETE",
    signal,
  });
  await parseJson<{ ok: boolean }>(response);
}

export async function startGitHubConnect(
  signal?: AbortSignal,
): Promise<RepoHostingConnectStartResponse> {
  const response = await apiFetch("/api/auth/repo-hosting/github/connect", {
    method: "POST",
    signal,
  });
  return parseJson<RepoHostingConnectStartResponse>(response);
}

export async function completeGitHubConnect(
  params: { code: string; state: string },
  signal?: AbortSignal,
): Promise<RepoHostingConnectCallbackResponse> {
  const search = new URLSearchParams(params).toString();
  const response = await apiFetch(
    `/api/auth/repo-hosting/github/callback?${search}`,
    {
      signal,
      cache: "no-store",
    },
  );
  return parseJson<RepoHostingConnectCallbackResponse>(response);
}

export async function fetchGitHubRepositories(
  signal?: AbortSignal,
): Promise<GitHubRepositoryListResponse> {
  const response = await apiFetch("/api/auth/repo-hosting/github/repos", {
    signal,
    cache: "no-store",
  });
  return parseJson<GitHubRepositoryListResponse>(response);
}

export function redirectBrowser(url: string): void {
  window.location.assign(url);
}
