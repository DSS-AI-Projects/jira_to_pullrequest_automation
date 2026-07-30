# jira2pullreq — Ticket → Plan → (optional) Implement

A web app that reads a Jira ticket, analyzes a Git repo, and produces a structured
implementation plan. For **local** repositories the user can then approve a second
phase that implements the plan in an isolated clone, captures the diff, and runs
validation. Opening a pull request is still out of scope.

> **Scope note:** this project began as "Milestone 1: Ticket → Plan" (plan only,
> single-user, env/ambient auth, no OAuth, no apply step). It has since grown a
> per-user auth foundation, delegated Jira/GitHub OAuth, local-repo execution, and
> a plan → implement → validate phase. This document describes the code as it is
> now. Where a capability is deliberately still unbuilt, it is called out under
> **Scope** below.

## Definition of done (per change)

A signed-in user (or, with auth off, any local user) submits a real Jira ticket key
(or URL) and a repo identifier, and gets back a valid, schema-conforming plan JSON
rendered on the review screen. For an approved local repo they may run the implement
phase and get back a diff + validation results. Every failure path returns a typed,
user-safe error, and all quality gates (types, lint, tests, secret scan) are green.

## Hard security principle: the app NEVER custodies user secrets *typed into the UI*

The web form collects **only non-secret identifiers** (ticket key/URL, repo
identifier). No credential field exists or will be added. Credentials reach the
server only through env config or an OAuth redirect the user completes with the
provider — never by typing a secret into this app's forms. Sources:

- **Jira — two coexisting modes:**
  - *Shared server credentials:* `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`
    from env (local dev uses a gitignored `.env`). The token is read **lazily,
    inside the Jira fetch step only** (`backend/app/steps/jira_auth.py`) — never held
    in app-wide state.
  - *Delegated per-user OAuth* (`JIRA_OAUTH_ENABLED`, `JIRA_OAUTH_CLIENT_ID/SECRET`,
    `JIRA_OAUTH_CALLBACK_URL`, `JIRA_OAUTH_ENCRYPTION_KEY`): Atlassian 3LO. Per-user
    access tokens are **encrypted at rest** (Fernet, `backend/app/core/crypto.py`)
    in SQLite and used preferentially, falling back to shared creds when a user has
    not connected. `JIRA_BASE_URL` still selects which Jira Cloud site to target.
- **GitHub — delegated OAuth** (`GITHUB_OAUTH_*`): per-user tokens encrypted at rest,
  used **for repository discovery / quick-picks only**. The actual `git clone` still
  inherits the machine's ambient git auth (SSH key / credential helper); delegated
  git auth is not built yet. GitLab has a connection-model foundation but no flow.
- **Anthropic:** `ANTHROPIC_API_KEY` from env, consumed only by the Agent SDK steps.

Security-architecture changes require the owner's sign-off — propose, don't implement.

## Application auth (optional, off by default)

`AUTH_ENABLED=false` by default; existing single-user local dev is unchanged. When
enabled (`backend/app/auth/`, `backend/app/api/auth.py`):

- Every job is stamped with the signed-in user as `owner_user_id`; non-admins can
  only read/implement their own jobs. `AUTH_ADMIN_EMAILS` grants cross-job access.
- Server-side sessions live in SQLite; the browser holds only an HTTP-only cookie.
- Two bootstrap modes: **dev login** (`AUTH_ALLOW_DEV_LOGIN`) and **trusted-proxy
  headers** (`AUTH_TRUSTED_PROXY_*`), the latter validated against an allowlisted
  source CIDR set before any identity header is trusted.

## Security invariants — each backed by a mechanical check

1. **No credential field on the form.** Test asserts the form schema and submit
   handler reject/ignore token-shaped input and never persist or log it.
2. **Secrets never logged.** A log redactor masks token-shaped strings and all known
   secret values; a test runs a job and asserts no secret value appears in logs.
3. **Secrets never enter the LLM context.** The agent receives only a local clone
   path (never a remote URL or credential), a repo map, and read/grep tools confined
   to the clone dir; the SDK subprocess gets a scrubbed environment. A test asserts
   no secret reaches the agent boundary (prompt, options, env, tool results).
4. **Secrets never in URLs, client error responses, or committed files.** Error
   responses are scrubbed to typed codes + safe messages; delegated OAuth tokens are
   encrypted at rest; gitleaks runs in CI and as a pre-commit hook.
5. **All inputs validated** (ticket key regex / Jira URL parse; repo URL shape +
   allowed-host list; local paths restricted to allowlisted roots). Ticket content
   and repo files are untrusted DATA, never instructions — quoted as data in the
   agent prompt, never spliced into system instructions. The same treatment applies
   to user-provided implementation clarifications (free text submitted alongside
   plan approval, `ImplementRequest.clarifications` — `extra="forbid"`, length- and
   credential-shape-checked like every other free-text field): stored on the job as
   `implementation_clarifications` and quoted into the implement prompt inside a
   `<user_clarifications>` tag, never treated as instructions and never able to
   widen scope beyond the approved plan.

## Deterministic work stays out of the LLM

Steps (a) Jira fetch, (b) git clone, (c) tree-sitter repo map are plain code — no LLM.
The LLM is used only via the harness below, in the **planning** step and the
**implementation** step; the post-implementation **validation** runner is again plain
code (auto-detected lint/type/test commands).

## Harness (do not hand-roll an agent loop)

`claude-agent-sdk` (Python, pinned in `pyproject.toml`). The **planning** step
(`steps/plan_agent.py`) uses `output_format={"type": "json_schema", "schema":
Plan.model_json_schema()}`; the SDK validates and re-prompts and surfaces
`error_max_structured_output_retries` on failure. The **implementation** step
(`steps/implement_agent.py`) runs on the same harness against the isolated workspace
clone. Agent config:

- Planning tools: read/grep/glob only, cwd = the clone dir; no Bash, no network tools.
- Per-phase config: planning and implementation have independent model, turn, and
  budget settings (`AGENT_PLAN_*` / `AGENT_IMPLEMENT_*`) so planning can run on a
  cheaper tier; shared `AGENT_EFFORT` controls reasoning depth.
- Budget: `max_turns` cap, `max_budget_usd` cost cap (native SDK option), and a
  wall-clock timeout via `asyncio.wait_for`. Budget exhaustion is `BUDGET_EXCEEDED`.
- Record tokens (incl. cache read/creation), cost, and duration from the SDK
  `ResultMessage` onto the job record (`usage` / `implementation_usage`).
- Malformed/invalid plan output: retry ONCE, then fail with typed `PLAN_INVALID`.

**Token-saving controls (planning step):** all opt-in via env, accuracy-preserving
first: `AGENT_PLAN_STUB` returns a plan with no API call at all (zero-credit
pipeline/demo testing) — deterministic and ticket-shaped rather than one
static placeholder: it infers a ticket type from keywords, references real
files from the repo map (ranked by keyword relevance then symbol count), and
varies test strategy/risks/open questions per ticket (`steps/plan_stub.py`);
every stub plan carries an unmistakable marker so it is never confused with a
real, model-generated plan; `AGENT_PLAN_CACHE_ENABLED` (on by default) memoizes plans on a
hash of prompt+model+effort+schema so identical ticket+repo re-runs cost zero
tokens (`backend/app/steps/plan_cache.py`); the planner front-loads the repo's own
`CLAUDE.md`/`AGENTS.md`/`README` (capped by `AGENT_REPO_DOC_MAX_CHARS`) so it needs
fewer exploration reads; and a **deterministic repo digest**
(`backend/app/steps/repo_digest.py`, zero tokens) — languages, top-level layout,
key files, core modules by symbol count, README excerpt — is computed once per
repo state, cached (`AGENT_REPO_DIGEST_*`), and injected into every plan so the
agent orients without exploring. Context management/compaction is handled by the
harness.

## The plan schema is a versioned contract

Defined ONCE as a Pydantic model (`backend/app/schemas/plan.py`) with `schema_version`
and fields: `summary`, `ticket_type`, `impacted_files[]`, `proposed_changes[]`,
`test_strategy`, `risks[]`, `open_questions[]`. `schema/plan.schema.json` (committed)
is exported from the model; `frontend/src/lib/plan.gen.ts` is generated from it. CI
regenerates both and fails on any diff. The implementation step consumes this object —
change it only with a `schema_version` bump.

## Failure behavior

Every step fails with a typed, user-safe error the status screen can display. The
catalog (`backend/app/core/errors.py`) currently covers, among others: `INPUT_INVALID`;
auth — `UNAUTHENTICATED`, `FORBIDDEN`, `AUTH_NOT_AVAILABLE`; Jira — `JIRA_CONFIG_MISSING`,
`JIRA_AUTH_FAILED`, `JIRA_UNREACHABLE`, `JIRA_OAUTH_*`, `JIRA_SITE_NOT_ACCESSIBLE`,
`TICKET_NOT_FOUND`, `TICKET_EMPTY`; repo providers — `REPO_PROVIDER_*`; repo/clone —
`REPO_HOST_NOT_ALLOWED`, `LOCAL_REPO_*` (not-allowed / not-found / not-directory /
outside-root / not-git / dirty / branch-mismatch), `CLONE_FAILED`, `REPO_MAP_FAILED`;
planning — `AGENT_CONFIG_MISSING`, `AGENT_REQUEST_FAILED`, `PLAN_INVALID`,
`BUDGET_EXCEEDED`; implementation — `IMPLEMENTATION_NOT_READY`,
`IMPLEMENTATION_NOT_SUPPORTED`, `IMPLEMENTATION_WORKSPACE_MISSING`, `VALIDATION_FAILED`;
and `INTERNAL`. Raw stack traces never reach the client.

Every job ends in a terminal state. Two async pipelines drive it:

- **Plan** (`run_job`): `QUEUED → FETCHING_TICKET → CLONING_REPO → MAPPING_REPO →
  PLANNING → PLAN_READY | FAILED`.
- **Implement** (`run_implementation`, opt-in, local repos only): `PLAN_READY →
  IMPLEMENTATION_QUEUED → IMPLEMENTING → VALIDATING → IMPLEMENTATION_READY |
  IMPLEMENTATION_FAILED`.

## Repository input & local execution

The `repo` field accepts a pre-configured repo name, a remote Git URL (host
allowlist), or — when `ALLOW_LOCAL_REPOS=true` — an absolute local path. Local paths
must be under an allowlisted root (`ALLOWED_LOCAL_REPO_ROOTS`), point to a Git work
tree, and are **cloned into a per-job workspace** (never mutated in place). Dirty
repos are rejected unless `ALLOW_DIRTY_LOCAL_REPOS`; `REQUIRE_LOCAL_BRANCH_TICKET_MATCH`
optionally requires the branch name contain the ticket key. `RepoInfo` (source kind,
branch, commit SHA, origin URL, dirty flag) is captured on the job.

The implement phase is **gated to local repos**. It records a pre-implementation
baseline git SHA, runs the implementation agent, diffs the workspace against the
baseline (`ImplementationDiff`, per-file patches + numstat), then runs the validation
runner. Diff collection and validation are best-effort — their failures degrade
gracefully rather than crashing the job.

Approving implementation may include optional free-text **clarifications** —
answers to the plan's `open_questions` or other guidance — submitted alongside
`POST /jobs/{id}/implement`. When present, they are quoted into the implement
prompt as untrusted data and the system prompt nudges the model to explicitly
acknowledge how each one was addressed in its summary; they never widen scope
beyond the approved plan. The submitted text is echoed back on the job
(`implementation_clarifications`) so the result view can show what was
considered.

## Scope

**In:** non-secret form; optional multi-user auth (dev login + trusted proxy);
delegated Jira/GitHub OAuth with encrypted-at-rest tokens; async jobs with SQLite
store + polling status screen; the plan pipeline (fetch/clone/map/plan); local-repo
execution with an isolated-clone implement + validate phase; plan and diff review
screens; typed errors; quality gates; security-invariant tests; a reference
shared-deployment stack under `deploy/`.

**Out (not built; do not scaffold):** opening a pull request / pushing branches;
delegated *git* auth (clone still uses ambient credentials); the GitLab OAuth flow;
implementing against *remote* repos; a durable workflow engine; a network-locked
sandbox; an embeddings/vector index.

## Stack

- Frontend: Next.js (App Router) + React + TypeScript strict; eslint + prettier;
  Vitest. Talks only to the backend API; OAuth callback routes under `src/app/auth/`.
- Backend: FastAPI (Python 3.12+), jobs as in-process `asyncio` tasks, SQLite store
  (jobs, users, sessions, encrypted provider tokens); ruff + pyright; pytest.
- Repo map: tree-sitter via `tree-sitter-language-pack` (Python, TS/JS, Java, Go,
  C#, Rust; other files appear in the tree without symbols).
- Jira: Cloud REST API v3 — Basic auth (shared) or OAuth bearer (delegated).
- Crypto: Fernet for provider tokens at rest.
- Deploy: `deploy/` holds an nginx + oauth2-proxy reference stack, Dockerfiles, and
  provider setup docs (e.g. Azure Entra ID).

## Quality gates — all must pass before merge

`python scripts/check.py` (and CI) runs: pyright, ruff check + format check, pytest,
tsc --noEmit, eslint, prettier check, plan-schema drift check, gitleaks. Tests cover
each job step, the plan schema, the malformed-plan rejection path, the security
invariants, auth/ownership, local-repo handling, and the implement/validate phase.
Small, reviewable commits after each working step.

## Conventions

- `.env` is gitignored; `.env.example` lists variable NAMES only, never values.
- Never print or log env values or provider tokens; always go through the redactor.
- Clones/workspaces go under a gitignored `var/workdir/`; the SQLite DB under `var/`.
- Frontend never talks to Jira/Git/Anthropic — only to the backend API.
