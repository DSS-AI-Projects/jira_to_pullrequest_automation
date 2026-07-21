# jira2pullreq

Reads a Jira ticket, analyzes a Git repo, and produces a structured implementation
plan (Milestone 1). Later milestones will apply the change and open a PR.

See [CLAUDE.md](CLAUDE.md) for the full project contract: scope, security
invariants, plan schema, failure policy, and quality gates.

## Security model (short version)

The app **never custodies user secrets**. The web form accepts only non-secret
identifiers (ticket key/URL, repo identifier). Jira auth comes from environment
variables (`backend/.env`, gitignored — see `backend/.env.example` for the names);
git clone uses your machine's ambient git auth (SSH key / credential helper);
the Anthropic key comes from env. No credential is ever typed into the UI, logged,
sent to the LLM, or committed.

When app authentication is enabled, the server does store non-secret user
metadata plus server-managed session ids in its SQLite database so it can enforce
job ownership. External identity provider credentials still stay outside the app.

## Authentication (Milestone M1)

The app now supports a provider-agnostic auth foundation for multi-user
deployment. It is off by default, so existing single-user development remains
unchanged until you opt in.

- `AUTH_ENABLED=true` turns on authenticated access for the API and frontend.
- Every created job is stamped with the signed-in user as `owner_user_id`.
- Non-admin users can only read and implement their own jobs.
- `AUTH_ADMIN_EMAILS=[...]` grants cross-job access for operator/admin accounts.
- Session state is stored server-side and sent to the browser as an HTTP-only
  cookie.

Two bootstrap modes are supported:

- Dev login: enable `AUTH_ALLOW_DEV_LOGIN=true` to use the built-in local sign-in
  form for development.
- Trusted proxy headers: if your reverse proxy or SSO gateway injects identity
  headers, the backend can bootstrap a local session from them, but only when
  trusted-proxy mode is explicitly enabled and the request comes from an
  allowlisted proxy source.

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

For local development over plain HTTP, leave `AUTH_SESSION_COOKIE_SECURE=false`.
For HTTPS deployments behind a reverse proxy, set it to `true`.

For shared deployments behind SSO or an auth gateway:

- Set `AUTH_ALLOW_DEV_LOGIN=false`.
- Set `AUTH_TRUSTED_PROXY_ENABLED=true`.
- Restrict `AUTH_TRUSTED_PROXY_SOURCES` to the proxy's source IPs or CIDR ranges.
- Configure the proxy to strip any incoming `X-Auth-Request-*` headers from the
  client and inject its own trusted identity headers instead.
- Prefer a same-origin reverse proxy that serves the frontend and forwards
  `/api/` to the backend, so the browser does not need a separate API origin.
- See [deploy/README.md](file:///d:/work/2026/AI/jira2pullreq/deploy/README.md)
  and [oauth2-proxy.conf.example](file:///d:/work/2026/AI/jira2pullreq/deploy/nginx/oauth2-proxy.conf.example)
  for a concrete `nginx + oauth2-proxy` example.
- The deployment bundle also includes
  [deploy/.env.shared-auth.example](file:///d:/work/2026/AI/jira2pullreq/deploy/.env.shared-auth.example),
  [oauth2-proxy.env.example](file:///d:/work/2026/AI/jira2pullreq/deploy/oauth2-proxy/oauth2-proxy.env.example)
  plus the Microsoft-specific
  [azure-entra.env.example](file:///d:/work/2026/AI/jira2pullreq/deploy/oauth2-proxy/azure-entra.env.example),
  and
  [docker-compose.shared-auth.example.yml](file:///d:/work/2026/AI/jira2pullreq/deploy/docker-compose.shared-auth.example.yml)
  as a reference shared-deployment stack.
- A provider-specific setup guide is available in
  [AZURE_ENTRA_ID.md](file:///d:/work/2026/AI/jira2pullreq/deploy/AZURE_ENTRA_ID.md).
- Built-image packaging is also included via
  [backend/Dockerfile](file:///d:/work/2026/AI/jira2pullreq/backend/Dockerfile),
  [frontend/Dockerfile](file:///d:/work/2026/AI/jira2pullreq/frontend/Dockerfile),
  and the compose-specific nginx example in
  [oauth2-proxy.compose.conf.example](file:///d:/work/2026/AI/jira2pullreq/deploy/nginx/oauth2-proxy.compose.conf.example).

## Local Repo Support

The backend can optionally accept a local repository path in the same `repo`
field used for preconfigured repos and remote Git URLs.

- Local repo support is disabled by default.
- Local repo paths must be absolute Windows paths.
- Local repo paths must point to a Git working tree.
- Local repo paths must be under one of the allowlisted local roots.
- Local repo inputs are cloned into the per-job workspace; the original checkout
  is not used in place.
- Dirty local repos are rejected by default.
- Local branch names can optionally be required to include the Jira ticket key.
- Remote repo URL support is unchanged.

When local repo support is enabled, the job response also includes repo metadata
captured from the selected branch/worktree: branch, commit SHA, origin URL, and
dirty/clean state.

Configure this in `backend/.env` when needed:

- `ALLOW_LOCAL_REPOS=true`
- `ALLOWED_LOCAL_REPO_ROOTS=["D:\\repos","D:\\workspaces"]`
- `ALLOW_DIRTY_LOCAL_REPOS=false`
- `REQUIRE_LOCAL_BRANCH_TICKET_MATCH=true`

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

If you enable auth locally, update `backend/.env` with the auth settings above
before starting the backend.

## Run (dev)

```sh
# terminal 1 — backend on :8000
cd backend && uv run uvicorn app.main:app --reload

# terminal 2 — frontend on :3000
cd frontend && npm run dev
```

## Quality gates

Everything must pass before merge. One command, cross-platform:

```sh
python scripts/check.py
```

Runs: pyright, ruff (lint + format), pytest, tsc, eslint, prettier check,
plan-schema drift check, gitleaks. CI (`.github/workflows/ci.yml`) runs the same
script. Backend checks also run from `backend/.venv` when present, so local
validation works even if `uv` is not installed globally.
