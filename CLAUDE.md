# jira2pullreq — Milestone 1: Ticket → Plan

A web app that reads a Jira ticket, analyzes a Git repo, and produces a structured
implementation plan. Later milestones will apply the change in a sandbox and open a
PR — **those are out of scope now and must not be scaffolded**.

## Definition of done (Milestone 1)

The user submits a real Jira ticket key (or URL) and a repo identifier, and gets back
a valid, schema-conforming plan JSON rendered on the review screen — with typed,
user-safe errors on every failure path, and all quality gates (types, lint, tests,
secret scan) green. Nothing else.

## Hard security principle: the app NEVER custodies user secrets

The web form collects **only non-secret identifiers** (ticket key/URL, repo
identifier). No credential field exists or will be added. Auth is sourced without the
user ever typing a secret:

- **Jira (Cloud, REST v3):** base URL, email, API token read at runtime from
  environment variables (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`; local dev
  uses a gitignored `.env`). The token is loaded **lazily, inside the Jira fetch step
  only** — never held in app-wide state. The auth-provider seam
  (`backend/app/steps/jira_auth.py`) is the single place to swap in OAuth later;
  **do not build OAuth now.**
- **Git:** `git clone` inherits the machine's ambient auth (SSH key / credential
  helper). The app never reads, stores, or handles a Git token.
- **Anthropic:** `ANTHROPIC_API_KEY` from env, consumed only by the Agent SDK step.

Env/ambient auth is a deliberate architecture decision by the project owner for this
local, single-user milestone. Security-architecture changes require the owner's
sign-off — propose, don't implement.

## Security invariants — each backed by a mechanical check

1. **No credential field on the form.** Test asserts the form schema and submit
   handler reject/ignore token-shaped input and never persist or log it.
2. **Secrets never logged.** A log redactor masks token-shaped strings and all known
   secret env values; a test runs a job and asserts no secret value appears in
   captured logs.
3. **Secrets never enter the LLM context.** The agent receives only a local clone
   path (never a remote URL), a repo map, and read/grep tools confined to the clone
   dir; the SDK subprocess gets a scrubbed environment. A test asserts no secret
   value reaches the agent boundary (prompt, options, env, tool results).
4. **Secrets never in URLs, client error responses, or committed files.** Error
   responses are scrubbed to typed codes + safe messages; gitleaks runs in CI and as
   a pre-commit hook.
5. **All form inputs validated** (ticket key regex / Jira URL parse; repo URL shape +
   allowed-host list). Ticket content and repo files are untrusted DATA, never
   instructions — quote them as data in the agent prompt, never splice them into
   system instructions.

## Deterministic work stays out of the LLM

Steps (a) Jira fetch, (b) git clone, (c) tree-sitter repo map are plain code — no LLM
involvement. Only step (d), planning, uses the LLM, and only via the harness below.

## Harness (do not hand-roll an agent loop)

`claude-agent-sdk` (Python) — pin the version in `pyproject.toml` (targeting 0.2.114,
July 2026). Use `output_format={"type": "json_schema", "schema": Plan.model_json_schema()}`
for the plan; the SDK validates and re-prompts, and surfaces
`error_max_structured_output_retries` on failure. Agent config:

- Tools: read/grep/glob only, cwd = the clone dir; no Bash, no network tools.
- Budget: `max_turns` cap, wall-clock timeout via `asyncio.wait_for`, and a max
  file-read count enforced in tool permission hooks. Budget exhaustion is the typed
  error `BUDGET_EXCEEDED`.
- Record tokens used and duration from the SDK `ResultMessage` onto the job record.
- Malformed/invalid plan output: retry ONCE, then fail with typed `PLAN_INVALID`.

## The plan schema is a versioned contract

Defined ONCE as a Pydantic model (`backend/app/schemas/plan.py`) with
`schema_version`, and fields: `summary`, `ticket_type`, `impacted_files[]`,
`proposed_changes[]`, `test_strategy`, `risks[]`, `open_questions[]`.
`schema/plan.schema.json` (committed) is exported from the Pydantic model; the
frontend type (`frontend/src/lib/plan.gen.ts`) is generated from that JSON Schema.
CI regenerates both and fails on any diff, so backend and frontend cannot drift.
This object is the API the future implement step will consume — change it only with
a `schema_version` bump.

## Failure behavior

Every job step fails with a typed, user-safe error the status screen can display:
`JIRA_CONFIG_MISSING`, `JIRA_AUTH_FAILED`, `JIRA_UNREACHABLE`, `TICKET_NOT_FOUND`,
`TICKET_EMPTY`,
`INPUT_INVALID`, `REPO_HOST_NOT_ALLOWED`, `CLONE_FAILED`, `REPO_MAP_FAILED`,
`PLAN_INVALID`, `BUDGET_EXCEEDED`, `INTERNAL`. Raw stack traces never reach the
client. Every job ends in a terminal state — `PLAN_READY` or `FAILED` with a reason.
Job states: `QUEUED → FETCHING_TICKET → CLONING_REPO → MAPPING_REPO → PLANNING →
PLAN_READY | FAILED`.

## Scope

**In:** form (non-secret inputs only), async job with SQLite store + polling status
screen, the four job steps above, plan review screen, typed errors, quality gates,
security-invariant tests.

**Out (do not build or scaffold):** implement/apply step, OAuth, provider App
install, durable workflow engine, network-locked sandbox, multi-repo management UI,
embeddings/vector index.

## Stack

- Frontend: Next.js (App Router) + React + TypeScript strict; eslint + prettier.
- Backend: FastAPI (Python 3.12+), jobs as in-process `asyncio` tasks, SQLite job
  store; ruff (lint + format) + pyright; pytest.
- Repo map: tree-sitter via `tree-sitter-language-pack` (Python, TS/JS, Java, Go,
  C#, Rust; other files appear in the tree without symbols).
- Jira: Cloud REST API v3, Basic auth (email + API token).

## Quality gates — all must pass before merge

`make check` (and CI) runs: pyright, ruff check + format check, pytest, tsc
--noEmit, eslint, prettier check, schema-drift check, gitleaks. Unit tests cover
each job step, the plan schema, the malformed-plan rejection path, and the five
security invariants. Small, reviewable commits after each working step.

## Conventions

- `.env` is gitignored; `.env.example` lists variable NAMES only, never values.
- Never print or log env values; always go through the redacting logger.
- Clones go under a gitignored `var/workdir/`; the SQLite DB under `var/`.
- Frontend never talks to Jira/Git/Anthropic — only to the backend API.
