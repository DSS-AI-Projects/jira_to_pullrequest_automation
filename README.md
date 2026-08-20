# jira2pullreq

Reads a Jira ticket — or an uploaded PDF requirement document — analyzes a Git
repo, and produces a structured, schema-validated implementation plan. For
**local** repositories, an approved plan can then be implemented in an
isolated clone, producing a real diff plus lint/type/test validation results.
Opening a pull request is still out of scope.

See [CLAUDE.md](CLAUDE.md) for the full project contract: scope, security
invariants, plan schema, failure policy, and quality gates. This README is a
quick-start and setup reference; CLAUDE.md is the authoritative description of
what the app does and why.

## What it does

1. **Requirement input** — submit a Jira ticket key/URL, or upload a PDF
   requirement document as an alternative (see below). Exactly one is
   required.
2. **Repo analysis** — the target repo is cloned (or, for local sources,
   copied into an isolated per-job workspace) and mapped with tree-sitter.
   Both steps are plain, deterministic code — no LLM involved.
3. **Planning** — a Claude Agent SDK agent, restricted to read-only
   `Read`/`Grep`/`Glob` tools inside the clone, produces a schema-validated
   plan: summary, ticket type, story-point estimate, complexity level,
   impacted files, proposed changes, test strategy, risks, and open
   questions.
4. **Review** — the plan renders on a review screen for a human to approve.
5. **Implementation (local repos only, opt-in)** — once approved, a second
   agent applies the plan inside the isolated clone (`Read`/`Grep`/`Glob`/
   `Edit`/`Write`, no `Bash`, no network tools), producing a real diff against
   a pre-implementation baseline git SHA, followed by an auto-detected
   lint/type/test validation run (plain code, not an LLM step).
6. **Job history & cost tracking** — every job is listed on a paginated
   `/jobs` page scoped to its owner; admins get a per-user AI cost breakdown
   at `/admin/costs`.

Every job ends in a terminal state, and every failure path returns a typed,
user-safe error — no raw stack traces reach the client.

## Security model (short version)

The web form accepts only non-secret identifiers (ticket key/URL, repo
identifier) — or, in place of a ticket, an uploaded PDF whose extracted text
is treated as untrusted data exactly like ticket content. In shared Jira
mode, Jira auth comes from environment variables (`backend/.env`, gitignored
— see `backend/.env.example` for the names); git clone uses your machine's
ambient git auth (SSH key / credential helper); the Anthropic key comes from
env. No credential is ever typed into the UI, logged, sent to the LLM, or
committed.

When app authentication is enabled, the server stores non-secret user
metadata plus server-managed session ids in SQLite so it can enforce job
ownership. In delegated Jira/GitHub mode, the backend also stores
user-specific OAuth tokens encrypted at rest (Fernet) in SQLite. Those tokens
still stay out of the browser forms, logs, and LLM prompts.

## Authentication

The app supports a provider-agnostic auth foundation for multi-user
deployment. It is off by default, so existing single-user development remains
unchanged until you opt in.

- `AUTH_ENABLED=true` turns on authenticated access for the API and frontend.
- Every created job is stamped with the signed-in user as `owner_user_id`.
- Non-admin users can only read and implement their own jobs.
- `AUTH_ADMIN_EMAILS=[...]` grants cross-job access and the admin cost
  dashboard.
- Session state is stored server-side and sent to the browser as an HTTP-only
  cookie.

Two bootstrap modes are supported:

- Dev login: enable `AUTH_ALLOW_DEV_LOGIN=true` to use the built-in local
  sign-in form for development.
- Trusted proxy headers: if your reverse proxy or SSO gateway injects
  identity headers, the backend can bootstrap a local session from them, but
  only when trusted-proxy mode is explicitly enabled and the request comes
  from an allowlisted proxy source.

Relevant settings in `backend/.env`:

- `AUTH_ENABLED=true`
- `AUTH_ALLOW_DEV_LOGIN=true`
- `AUTH_SESSION_COOKIE_NAME=jira2pullreq_session`
- `AUTH_SESSION_TTL_HOURS=12`
- `AUTH_SESSION_COOKIE_SECURE=false`
- `AUTH_TRUSTED_PROXY_ENABLED=false`
- `AUTH_TRUSTED_PROXY_SOURCES=["127.0.0.1/32","::1/128"]`
- `AUTH_TRUSTED_EMAIL_HEADER=X-Auth-Request-Email`
- `AUTH_TRUSTED_NAME_HEADER=X-Auth-Request-Name`
- `AUTH_TRUSTED_SUBJECT_HEADER=X-Auth-Request-User`
- `AUTH_TRUSTED_PROVIDER_NAME=trusted-proxy`
- `AUTH_ADMIN_EMAILS=["admin@example.com"]`

## Requirement source: Jira ticket or uploaded document

`POST /api/jobs` accepts **exactly one** of a Jira ticket key/URL or an
uploaded PDF requirement document — enforced server-side, mirrored by a
toggle on the job form.

- Text is extracted from the PDF (`pypdf`) deterministically — no LLM
  involved — and quoted into the planning prompt as untrusted data through
  the same path Jira ticket content uses.
- A document-sourced job gets a synthetic `DOC-XXXXXXXX` ticket key instead
  of a real Jira key, and skips the local-branch/ticket-match check (a
  synthetic key can never appear in a real branch name).
- Upload safeguards: a magic-byte check (rejects anything that isn't
  actually a PDF regardless of filename/content-type), a size cap
  (`DOCUMENT_MAX_UPLOAD_BYTES`, 10MB default), a parse timeout
  (`DOCUMENT_PARSE_TIMEOUT_SECONDS`), and a truncation cap on extracted text
  (`DOCUMENT_MAX_EXTRACTED_CHARS`) so a huge PDF can't blow the planning
  budget.
- The uploaded file is not persisted across a retry — retrying a failed
  document-sourced job requires re-uploading it.

## Jira access modes

Two Jira auth modes coexist so shared and multi-user deployments can migrate
incrementally:

- Shared server credentials: configure `JIRA_BASE_URL`, `JIRA_EMAIL`, and
  `JIRA_API_TOKEN` for the existing single-account server-side Jira access
  path.
- Delegated per-user access: enable Atlassian OAuth via
  `JIRA_OAUTH_ENABLED=true`, `JIRA_OAUTH_CLIENT_ID`,
  `JIRA_OAUTH_CALLBACK_URL`, `JIRA_OAUTH_CLIENT_SECRET`, and
  `JIRA_OAUTH_ENCRYPTION_KEY`.
- For the built-in frontend UX, set `JIRA_OAUTH_CALLBACK_URL` to the public
  frontend callback route, for example
  `http://localhost:3000/auth/jira/callback` in local dev or
  `https://app.example.com/auth/jira/callback` behind a shared deployment
  proxy.
- Delegated access is stored per signed-in user and used preferentially
  during Jira fetches. If a user has not connected Jira yet, the backend
  still falls back to the shared server credentials when those are
  configured.
- `JIRA_BASE_URL` remains important in delegated mode because it identifies
  which Jira Cloud site this server should target when a user can access
  more than one Atlassian site.

## Repository provider connections

Per-user GitHub OAuth connections are supported for repository hosting
(GitLab has a connection-model foundation but no OAuth flow yet).

- The backend implements the GitHub OAuth flow with encrypted-at-rest token
  storage (Fernet).
- The frontend provides Connect/Disconnect UI and callback redirect
  handling; a stored token that can no longer be decrypted (e.g. after an
  encryption-key rotation) degrades gracefully to the same "not connected"
  state rather than surfacing a raw error.
- The job form can show quick-picks of repositories from a connected GitHub
  account.
- Config requirements in `backend/.env`: `GITHUB_OAUTH_ENABLED=true`,
  `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`,
  `GITHUB_OAUTH_CALLBACK_URL` (frontend route), `GITHUB_OAUTH_ENCRYPTION_KEY`.
- Like Jira delegated mode, GitHub OAuth tokens are stored encrypted per
  signed-in user and never exposed to the UI, logs, or LLM.
- **Currently the GitHub connection is used for repo discovery only** — the
  actual `git clone` still uses your local machine's ambient git
  credentials. Delegated git auth (and PR/branch creation) is planned but
  not yet built.

## Repository input & local execution

The `repo` field accepts a pre-configured repo name, a remote Git URL (host
allowlist), or — when `ALLOW_LOCAL_REPOS=true` — an absolute local path.

- Local paths must be under one of the allowlisted local roots.
- By default a local path must point to a Git work tree; dirty repos are
  rejected unless `ALLOW_DIRTY_LOCAL_REPOS=true`, and
  `REQUIRE_LOCAL_BRANCH_TICKET_MATCH=true` can optionally require the branch
  name to contain the ticket key.
- A separate opt-in, `ALLOW_LOCAL_NON_GIT_FOLDERS=true`, additionally accepts
  a plain source folder with no `.git` — since there's no git history to
  check, branch/dirty checks don't apply, and this is a weaker guarantee
  than a real git work tree.
- **Local repo inputs are always cloned (or, for a non-git folder, copied)
  into an isolated per-job workspace — the original checkout is never
  mutated in place.** This holds for both the planning and implementation
  phases.
- When local repo support is enabled, the job response includes repo
  metadata captured from the selected branch/worktree: branch, commit SHA,
  origin URL, and dirty/clean state.

**Implementation phase** (local sources only — `LOCAL` or `LOCAL_FOLDER`;
remote repos are excluded): after plan approval, the implementation agent
applies the plan inside the isolated workspace. A pre-implementation baseline
git SHA is recorded so the diff capture step correctly captures changes even
as the implementation advances the workspace's git state; the diff (per-file
patches + numstat) and validation results (auto-detected lint/type/test
commands) are then attached to the job. **Applying an approved implementation
back to the original local checkout is a manual step** — copy the changed
files (or the downloaded patch) from the job's workspace
(`backend/var/workdir/<job-id>/repo/`) into your real checkout yourself; the
app does not write to it automatically.

Configure this in `backend/.env` when needed:

- `ALLOW_LOCAL_REPOS=true`
- `ALLOWED_LOCAL_REPO_ROOTS=["D:\\repos","D:\\workspaces"]`
- `ALLOW_DIRTY_LOCAL_REPOS=false`
- `REQUIRE_LOCAL_BRANCH_TICKET_MATCH=true`
- `ALLOW_LOCAL_NON_GIT_FOLDERS=false`

## Prerequisites

- Python ≥ 3.12, Node ≥ 22, git
- [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- [gitleaks](https://github.com/gitleaks/gitleaks) (`winget install Gitleaks.Gitleaks` / `brew install gitleaks`)
- [pre-commit](https://pre-commit.com/) (`pip install pre-commit`), then `pre-commit install`

## Setup

```sh
# backend
cd backend
uv sync
cp .env.example .env   # then fill in values — never commit .env

# frontend
cd ../frontend
npm install
```

If you enable auth locally, update `backend/.env` with the auth settings
above before starting the backend.

## Run (dev)

```sh
# terminal 1 — backend on :8000 (default)
cd backend && uv run uvicorn app.main:app --reload

# Or use port 8010 to avoid stale listeners:
# cd backend && uv run uvicorn app.main:app --reload --host localhost --port 8010

# terminal 2 — frontend on :3000
cd frontend && npm run dev
```

If you run the backend on port 8010 locally, set
`NEXT_PUBLIC_API_BASE_URL=http://localhost:8010` in the frontend environment,
or set the backend to listen on `localhost:8000`.

To generate plans without spending Anthropic credits (e.g. to smoke-test the
whole pipeline), set `AGENT_PLAN_STUB=true` — every stub plan carries an
unmistakable marker so it's never confused with a real, model-generated plan.

## Quality gates

Everything must pass before merge. One command, cross-platform:

```sh
python scripts/check.py
```

Runs: pyright, ruff (lint + format), pytest, tsc, eslint, prettier check,
plan-schema drift check, gitleaks. CI (`.github/workflows/ci.yml`) runs the
same script. Backend checks also run from `backend/.venv` when present, so
local validation works even if `uv` is not installed globally.

## Shared / production deployment

For local development over plain HTTP, leave `AUTH_SESSION_COOKIE_SECURE=false`.
For HTTPS deployments behind a reverse proxy, set it to `true`.

For shared deployments behind SSO or an auth gateway:

- Set `AUTH_ALLOW_DEV_LOGIN=false`.
- Set `AUTH_TRUSTED_PROXY_ENABLED=true`.
- Restrict `AUTH_TRUSTED_PROXY_SOURCES` to the proxy's source IPs or CIDR
  ranges.
- Configure the proxy to strip any incoming `X-Auth-Request-*` headers from
  the client and inject its own trusted identity headers instead.
- Prefer a same-origin reverse proxy that serves the frontend and forwards
  `/api/` to the backend, so the browser does not need a separate API
  origin (`NEXT_PUBLIC_API_BASE_URL` can then stay unset).

See [deploy/README.md](deploy/README.md) and
[deploy/nginx/oauth2-proxy.conf.example](deploy/nginx/oauth2-proxy.conf.example)
for a concrete `nginx + oauth2-proxy` example. The deployment bundle also
includes
[deploy/.env.shared-auth.example](deploy/.env.shared-auth.example),
[deploy/oauth2-proxy/oauth2-proxy.env.example](deploy/oauth2-proxy/oauth2-proxy.env.example)
plus the Microsoft-specific
[deploy/oauth2-proxy/azure-entra.env.example](deploy/oauth2-proxy/azure-entra.env.example),
and
[deploy/docker-compose.shared-auth.example.yml](deploy/docker-compose.shared-auth.example.yml)
as a reference shared-deployment stack. A provider-specific setup guide is
available in [deploy/AZURE_ENTRA_ID.md](deploy/AZURE_ENTRA_ID.md).

Production-oriented image builds are included via
[backend/Dockerfile](backend/Dockerfile) and
[frontend/Dockerfile](frontend/Dockerfile), plus the compose-specific nginx
example in
[deploy/nginx/oauth2-proxy.compose.conf.example](deploy/nginx/oauth2-proxy.compose.conf.example).
For a quick single-command local container run instead, see the root
[docker-compose.yml](docker-compose.yml).
