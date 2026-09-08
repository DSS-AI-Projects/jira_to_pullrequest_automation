# jira2pullreq — Ticket → Plan → (optional) Implement

A web app that reads a Jira ticket, analyzes a Git repo, and produces a structured
implementation plan. The user can then approve a second phase that implements the
plan in an isolated clone (local or remote repos alike), captures the diff, and
runs validation. Opening a pull request is still out of scope.

> **Scope note:** this project began as "Milestone 1: Ticket → Plan" (plan only,
> single-user, env/ambient auth, no OAuth, no apply step). It has since grown a
> per-user auth foundation, delegated Jira/GitHub OAuth, local-repo execution, and
> a plan → implement → validate phase. This document describes the code as it is
> now. Where a capability is deliberately still unbuilt, it is called out under
> **Scope** below.

## Definition of done (per change)

A signed-in user (or, with auth off, any local user) submits a real Jira ticket key
(or URL) — or, alternatively, uploads a PDF requirement document — plus a repo
identifier, and gets back a valid, schema-conforming plan JSON rendered on the
review screen. Once approved, they may run the implement phase — local or remote
repo alike — and get back a diff + validation results. Every failure path returns
a typed, user-safe error, and all quality gates (types, lint, tests, secret scan)
are green.

## Hard security principle: the app NEVER custodies user secrets *typed into the UI*

The web form collects **only non-secret identifiers** (ticket key/URL, repo
identifier) — or, as an alternative to a Jira ticket, an uploaded PDF requirement
document, whose extracted text is treated as untrusted DATA exactly like ticket
content (see **Requirement source: Jira ticket or uploaded document** below). No
credential field exists or will be added. Credentials reach the server only through
env config or an OAuth redirect the user completes with the provider — never by
typing a secret into this app's forms. Sources:

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
  used **for repository discovery / quick-picks only**. `git clone` and `git push`
  both still inherit the machine's ambient git auth (SSH key / credential helper);
  delegated *git* auth is not built yet. GitLab has a connection-model foundation
  but no flow.
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
   widen scope beyond the approved plan. A second, distinct entry point exists
   earlier in the flow: optional **planning notes** submitted alongside the ticket
   and repo on the job-creation form (the `planning_notes` form field, same
   length/credential-shape checks as every other free-text field), stored as
   `Job.planning_notes` and quoted into the planning prompt inside a
   `<user_technical_notes>` tag —
   technical constraints or context to shape the generated plan itself, not just
   the later implementation. It also participates in the plan cache key, since that
   key hashes the full rendered prompt.

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
and fields: `summary`, `ticket_type`, `estimated_story_points`, `complexity_level`,
`impacted_files[]`, `proposed_changes[]`, `test_strategy`, `risks[]`, `open_questions[]`.
`estimated_story_points` is a standard Fibonacci-like Scrum estimate (1/2/3/5/8/13/21)
and `complexity_level` is low/medium/high/very_high — both grounded in the scope of
`proposed_changes`, surfaced on the plan review screen so a developer can judge whether
a ticket should be split into smaller subtasks before implementation starts (the UI
flags high/very_high complexity explicitly). `schema/plan.schema.json` (committed)
is exported from the model; `frontend/src/lib/plan.gen.ts` is generated from it. CI
regenerates both and fails on any diff. The implementation step consumes this object —
change it only with a `schema_version` bump.

## Failure behavior

Every step fails with a typed, user-safe error the status screen can display. The
catalog (`backend/app/core/errors.py`) currently covers, among others: `INPUT_INVALID`;
auth — `UNAUTHENTICATED`, `FORBIDDEN`, `AUTH_NOT_AVAILABLE`; Jira — `JIRA_CONFIG_MISSING`,
`JIRA_AUTH_FAILED`, `JIRA_UNREACHABLE`, `JIRA_OAUTH_*`, `JIRA_SITE_NOT_ACCESSIBLE`,
`TICKET_NOT_FOUND`, `TICKET_EMPTY`; requirement document — `DOCUMENT_NOT_PDF`,
`DOCUMENT_TOO_LARGE`, `DOCUMENT_UNREADABLE`, `DOCUMENT_EMPTY`; repo providers —
`REPO_PROVIDER_*`; repo/clone —
`REPO_HOST_NOT_ALLOWED`, `LOCAL_REPO_*` (not-allowed / not-found / not-directory /
outside-root / not-git / dirty / branch-mismatch), `CLONE_FAILED`, `REPO_MAP_FAILED`;
planning — `AGENT_CONFIG_MISSING`, `AGENT_REQUEST_FAILED`, `PLAN_INVALID`,
`BUDGET_EXCEEDED`; implementation — `IMPLEMENTATION_NOT_READY`,
`IMPLEMENTATION_NOT_SUPPORTED`, `IMPLEMENTATION_WORKSPACE_MISSING`, `VALIDATION_FAILED`;
validation correction — `VALIDATION_CORRECTION_NOT_AVAILABLE`; and `INTERNAL`. Raw
stack traces never reach the client.

Every job ends in a terminal state. Two async pipelines drive it:

- **Plan** (`run_job`): `QUEUED → FETCHING_TICKET → CLONING_REPO → MAPPING_REPO →
  PLANNING → PLAN_READY | FAILED`.
- **Implement** (`run_implementation`, opt-in, any repo source): `PLAN_READY →
  IMPLEMENTATION_QUEUED → IMPLEMENTING → VALIDATING → IMPLEMENTATION_READY |
  IMPLEMENTATION_FAILED`.

## Requirement source: Jira ticket or uploaded document

`POST /api/jobs` accepts **exactly one** of `ticket` (a Jira key/URL) or an uploaded
`requirement_document` (PDF) — enforced server-side (`INPUT_INVALID` if both or
neither are present), mirrored client-side by a toggle on the job form. This is a
multipart request, not JSON, so the endpoint declares individual `Form()`/`File()`
parameters rather than a single Pydantic body model (mixing a Pydantic `Form()`
model with a sibling `File()` field does not bind correctly in this FastAPI
version — verified empirically, not assumed); the smuggled-extra-field protection
`extra="forbid"` normally gives up is reconstructed by checking the raw
`request.form()` keys against an explicit allowlist
(`reject_unknown_form_fields`).

A document-sourced job gets a synthetic `DOC-XXXXXXXX` ticket key (`Job.new()`,
`RequirementSource.DOCUMENT`) rather than a real Jira key, and skips
`REQUIRE_LOCAL_BRANCH_TICKET_MATCH` (a synthetic key can never appear in a real
branch name). Text extraction (`backend/app/steps/document_fetch.py`, `pypdf`) is
deterministic and LLM-free, exactly like the Jira fetch step, and produces the same
`TicketData` shape — the planner never knows or cares which source was used. A
small dispatcher (`app/steps/fetch_requirement`) routes to one step or the other
based on `Job.requirement_source`, so `JobSteps.fetch_ticket`'s signature and every
existing fake-step test are untouched. The extracted text is quoted into the
planning prompt as untrusted data via the same `<ticket_data>` block Jira content
uses — no new prompt-injection surface.

Upload safeguards: a magic-byte check (`%PDF-`) rather than trusting the
client-supplied filename/content-type, a size cap (`DOCUMENT_MAX_UPLOAD_BYTES`,
10MB default), a parse timeout, and a truncation cap on extracted text
(`DOCUMENT_MAX_EXTRACTED_CHARS`) so a huge PDF can't blow the planning budget. The
uploaded file is saved to `var/uploads/<job_id>/requirement.pdf` before the
background job starts (mirroring how `var/workdir/` holds clone workspaces) and is
never re-used across jobs — retrying a failed document-sourced job requires
re-uploading the file (sessionStorage can't persist a `File` object across the
Retry redirect; the job form shows which filename to re-upload).

## Jira ticket attachments (PDF only)

Opt-in (`JIRA_ATTACHMENT_FETCH_ENABLED`, off by default) text extraction from PDF
attachments on the Jira ticket itself, feeding the same token-saving front-loading
philosophy as the repo digest: the planner sees attachment content without the
agent needing to discover and read it via tools. Deliberately scoped to PDF only,
matching the document-upload feature's own v1 scoping decision — no images, Word,
or Excel parsing.

`jira_fetch.py` requests the `attachment` field alongside the existing
`summary,description,comment` fields, then `fetch_attachment_texts()` downloads
and extracts up to `JIRA_ATTACHMENT_MAX_COUNT` PDFs (each capped at
`JIRA_ATTACHMENT_MAX_BYTES_PER_FILE`, combined extracted text capped at
`JIRA_ATTACHMENT_MAX_TOTAL_CHARS`), reusing `document_fetch.extract_pdf_text()`
directly rather than duplicating PDF-parsing logic. Jira's `mimeType` metadata is
never trusted alone — every downloaded attachment is still magic-byte checked
(`%PDF-`) before parsing, the same principle as the uploaded-document path.

Attachment fetching is **best-effort and never fails the job**: a network error,
a non-PDF attachment, an oversized file, or a corrupt PDF is logged and simply
excluded, since attachments are supplementary context, not required input (the
one exception remains `TICKET_EMPTY` if the ticket has neither summary nor
description — attachments don't change that check). Extracted text lands on
`TicketData.attachments` (a `list[str]`, same shape as `comments`) and is quoted
into the planning prompt inside a `<ticket_attachments>` tag — untrusted data,
exactly like every other ticket-derived content. No plan-cache changes were
needed: the cache key already hashes the full rendered prompt, so attachment
content is automatically covered.

## Repository input & local execution

The `repo` field accepts a pre-configured repo name, a remote Git URL (host
allowlist), or — when `ALLOW_LOCAL_REPOS=true` — an absolute local path. Local paths
must be under an allowlisted root (`ALLOWED_LOCAL_REPO_ROOTS`) and are **cloned (or,
for a non-git folder, copied) into a per-job workspace** (never mutated in place).
By default a local path must point to a Git work tree; dirty repos are rejected
unless `ALLOW_DIRTY_LOCAL_REPOS`, and `REQUIRE_LOCAL_BRANCH_TICKET_MATCH` optionally
requires the branch name contain the ticket key. `RepoInfo` (source kind, branch,
commit SHA, origin URL, dirty flag) is captured on the job.

A separate opt-in, `ALLOW_LOCAL_NON_GIT_FOLDERS`, additionally accepts a plain source
folder with no `.git` (`RepoSourceKind.LOCAL_FOLDER`) — e.g. an unpacked source tree
with no version control. Since there is no git history to check, dirty-state and
branch/ticket-match checks don't apply; `RepoInfo.branch` is `None` for this source
kind. The workspace copy is `git init` + committed *inside the isolated workspace
only* (never in the original folder) so the implement phase's diff/validation
pipeline — which only ever inspects the workspace's own git state — works unchanged.
The copy step is a security control, not just a convenience: a plain folder has no
`.gitignore` enforcement, so `.env`-shaped files, `node_modules`, `.venv`, and similar
are always excluded, and the source's own `.gitignore` (if present) is additionally
honored on a best-effort basis — this keeps stray secrets out of the workspace the
planning/implementation agent can Read/Grep (invariant 3).

The implement phase is available for every source kind (`LOCAL`, `LOCAL_FOLDER`,
and `REMOTE`) — clone already lands every source in the same kind of isolated
workspace (`RepoSourceKind` only changes how that workspace was populated, not
what the implement/validate/correct/branch-prep steps do with it afterward), so
there was no technical reason to gate implementation to local sources; it's
available uniformly now. It records a pre-implementation baseline git SHA, runs the
implementation agent, diffs the workspace against the baseline (`ImplementationDiff`,
per-file patches + numstat), then runs the validation runner. Diff collection and
validation are best-effort — their failures degrade gracefully rather than crashing
the job.

**A failed implementation never discards real work that already landed in the
workspace.** If `implement_plan` (or a validation-correction pass) raises partway
through — a budget cap, a request failure, or the "agent claimed changes that never
landed" consistency check — `_refresh_diff_best_effort` (`app/jobs/runner.py`)
re-collects whatever diff exists between the workspace's current state and the
recorded baseline before the job is marked failed, and attaches it to
`Job.implementation_diff` if non-empty. This only ever adds information: for the
main implement phase the diff was otherwise never computed on that failure path;
for a failed correction pass it can only be a superset of the diff the *original*
successful implementation already had (correction only ever adds uncommitted edits
on top of the same baseline), so it can't regress a working job. The status screen
surfaces this as a distinct "Changes made before the failure" panel on an
`IMPLEMENTATION_FAILED` job — separate from, and explicitly not to be confused
with, the normal `IMPLEMENTATION_READY` result panel, since this diff was never
validated and the agent's own summary for it is not trusted (`implementation_result`
is deliberately left unset on this path).

The validation runner (`backend/app/steps/validation_runner.py`) auto-detects a
Python profile (`ruff`/`pytest`, run via the *backend's own* `sys.executable` — not
a venv belonging to the target repo) and a Node profile (`npm run lint`/`npm test`,
from `package.json` scripts). The isolated workspace clone is just the repo's git
tree — `node_modules` is never tracked by git, so a Node profile always installs
dependencies first (`npm ci` if a lockfile is present, else `npm install`) before
running lint/test; without this, any devDependency binary (`ng`, `eslint`,
`vitest`, ...) fails with "not recognized"/"command not found" every time, which
looks like a validation failure but isn't a real code defect. Install is
best-effort and skipped if `node_modules` already exists (e.g. the post-correction
revalidation reuses the same workspace); if install itself fails, the remaining
npm-based checks are reported `SKIPPED` rather than run (they'd fail identically),
and that failure is **not** eligible fodder for the validation-correction pass
below — no source edit can fix a missing dependency, so feeding it to the
corrective agent would just burn its one-shot budget on an unfixable prompt. On
Windows, launching `npm`/`npx`/etc. also requires resolving the executable via
`shutil.which()` to its full, extension-included path (`_resolve_executable`) —
`subprocess.run` without `shell=True` only auto-appends `.exe` when searching
PATH, so passing a bare name like `"npm"` fails even though it correctly resolves
to an `npm.cmd` shim.

Approving implementation may include optional free-text **clarifications** —
answers to the plan's `open_questions` or other guidance — submitted alongside
`POST /jobs/{id}/implement`. When present, they are quoted into the implement
prompt as untrusted data and the system prompt nudges the model to explicitly
acknowledge how each one was addressed in its summary; they never widen scope
beyond the approved plan. The submitted text is echoed back on the job
(`implementation_clarifications`) so the result view can show what was
considered.

## Validation-correction feedback loop

After the validate step lands a job on `IMPLEMENTATION_READY` with one or more
`FAILED` validation results, the developer may explicitly trigger **one** corrective
pass (`POST /jobs/{id}/correct-validation`) rather than editing the diff by hand.
This is an opt-in, explicitly-approved action — never automatic — matching the
app's existing "human approves every code-writing action" pattern for the plan and
implement steps. It is capped at exactly one attempt per job
(`implementation_correction_attempted`, checked server-side so a second call is
rejected with `VALIDATION_CORRECTION_NOT_AVAILABLE`) and **never regresses** a
working implementation: whatever the correction's outcome, the job always lands
back on `IMPLEMENTATION_READY`, never a failure state, so the original
`implementation_result` stays visible even if the fix attempt itself errors out.

- **Reuses the existing implementation agent**, not a new one: same harness, same
  tool set (`Read`/`Grep`/`Glob`/`Edit`/`Write`, no `Bash`/network), same model —
  `steps/implement_agent.py`'s `build_prompt`/`build_options` just switch to a
  corrective framing when passed the job's `FAILED` validation results, and to
  tighter budget caps (`AGENT_IMPLEMENT_CORRECTION_MAX_TURNS`,
  `AGENT_IMPLEMENT_CORRECTION_MAX_BUDGET_USD`) than the main implementation pass.
  Validation output (`output_excerpt`, real subprocess output) is quoted into the
  prompt inside a `<validation_failures>` tag — untrusted data, same treatment as
  ticket/repo content and clarifications.
- **One cumulative diff, not a merge.** The pre-implementation baseline SHA is
  persisted on the job (`implementation_baseline_commit_sha`) the first time
  implementation runs, so the correction pass can re-diff the workspace against
  that same baseline afterward and simply overwrite `implementation_diff` with the
  result — original change plus fix, with no diff-merging logic required.
- **State machine:** two additional non-terminal `JobState` values, `CORRECTING`
  (agent applying a fix) and `REVALIDATING` (re-running the validation runner),
  both of which always resolve back to `IMPLEMENTATION_READY`
  (`run_validation_correction` in `app/jobs/runner.py`). They are deliberately
  **not** added to the frontend's linear pipeline stepper (`JOB_STATES` in
  `frontend/src/lib/job.ts`) — correction is a post-hoc addendum after
  `IMPLEMENTATION_READY`, not a continuation of the plan → implement pipeline —
  and are instead shown as a separate "Attempt automatic fix" card on the job
  status screen, alongside the correction's own summary and changed-files list
  once attempted.
- Deterministic work stays out of the LLM here too: re-collecting the diff and
  re-running validation after the corrective agent pass are the same plain-code
  steps (`_collect_implementation_diff`, the validation runner) the original
  implement phase already uses — only the corrective agent turn is new.

## Branch preparation and push

Once a job is `IMPLEMENTATION_READY`, the developer may explicitly create a branch
and commit the already-reviewed diff **inside the isolated workspace**
(`POST /jobs/{id}/create-branch`, `backend/app/steps/branch_prep.py`), then,
separately, push that branch to the repo's real remote
(`POST /jobs/{id}/push-branch`, same module). Like every other code-writing action
in this app, both are explicitly triggered by the user, never automatic.

- **Deterministic, synchronous, no LLM.** Branch/commit is plain git plumbing
  (`checkout -B`, `add -A`, `commit`) — unlike implement/validate/correct there's no
  slow agent call, so this endpoint runs inline and returns its result directly
  (200, not 202 + poll) instead of going through the async job-state-machine
  pattern the other mutating endpoints use.
- **Branch naming:** `jira2pullreq/<TICKET-KEY>` for Jira-sourced jobs;
  `jira2pullreq/<synthetic-key>-<slugified-plan-summary>` for a document-sourced job,
  since its synthetic `DOC-XXXXXXXX` key alone means nothing to a reviewer
  (`default_branch_name`). Both the branch name and the commit message (default:
  `<ticket-key>: <plan summary>`) can be overridden per request
  (`CreateBranchRequest`, same free-text length/credential-shape checks as every
  other user-provided field).
- **Branch-name validation is delegated to git itself** (`git check-ref-format
  --branch`) rather than reimplementing the ref-name grammar — a battle-tested tool
  already gets edge cases (double dots, trailing `.lock`, `@{`, control characters)
  right.
- **Capped at one successful call per job** (`Job.branch_name` set = done) — but
  only *after* success. A rejected name or an empty diff never touches git state, so
  those are always safely retryable with corrected input. This differs from
  validation correction's cap: there's no LLM budget to protect here, but retrying
  after a successful commit would mean re-pointing the branch at a fresh checkout of
  the baseline, discarding the working tree that first commit already absorbed — so
  the simplest safe design is to get it right once rather than support rename/retry.
- Reuses the same baseline SHA the implement/correction steps already establish
  (`implementation_baseline_commit_sha`) — since those steps only ever `git add`,
  never `git commit`, HEAD is still at the baseline when this step runs, so
  `checkout -B <name> <baseline>` is a no-op move that never touches the working
  tree holding the (uncommitted) implementation changes.

**Push (`push_branch()`) is a separate, later, explicitly-triggered step — and,
deliberately, the smallest change that could satisfy "push to GitHub" rather than
the larger delegated-credential design also considered:**

- **Ambient git auth only — no per-user credential is ever read or injected.**
  Exactly the same trust model `git clone` already uses (SSH key / credential
  helper on the machine running the backend). A shared multi-user deployment
  where individual developers don't have their own push access on that machine
  needs a separate, later capability (delegating the user's own connected GitHub
  OAuth token) — deliberately not built here; seeing this gap is the reason to
  build it, not a reason to work around it in the meantime.
- **Targets `Job.repo_info.origin_url`, never the workspace clone's own "origin"
  remote.** For a `LOCAL` job those are different things: `origin_url` was
  captured from the *original* local source's own git config before cloning (see
  `_inspect_repo` in `repo_clone.py`), i.e. the repo's real upstream (e.g.
  github.com) — while the workspace's own "origin" remote points at the local
  source path it was cloned from, since that's literally what `git clone
  <local_source> <dest>` sets it to. Pushing to the workspace's own "origin" for a
  `LOCAL` job would silently push into the user's own local checkout instead of
  their real remote — a `LOCAL_FOLDER` job (no real remote ever existed) or a
  `LOCAL` job with no configured origin correctly has `origin_url = None` and is
  rejected with `BRANCH_PUSH_NOT_AVAILABLE`.
- **Never force-pushes.** A `git ls-remote --heads` check runs before pushing; if
  a branch with that name already exists on the remote, the request is rejected
  (`BRANCH_PUSH_REJECTED`) rather than overwriting it.
- **Retryable, unlike branch creation** — there's no LLM budget to protect, and a
  failed push never touches the local branch/commit, so nothing is ever lost by
  trying again. A repeat push with no rename requested, on a job already pushed,
  is idempotent (re-reports the existing result) rather than re-attempting and
  incorrectly tripping the collision check against its own prior push. Pushing
  under a *different* name first does a local `git branch -m` (a safe rename —
  the branch is already committed, so nothing uncommitted is at risk) before
  pushing the new name — handles the case of retrying after a
  `BRANCH_PUSH_REJECTED` collision without starting a new job.
- **Never opens a pull request** — still out of scope. A successful push does
  return a plain `compare_url` web link (`https://github.com/<owner>/<repo>/compare/<branch>?expand=1`)
  when the remote is recognizably github.com, computed by `github_compare_url()`
  (mirrored client-side in `job-status-view.tsx` as `githubCompareUrl()` so the
  link still renders after a page reload, since it isn't persisted on the job) —
  it's a normal link the user clicks themselves, never an API call this app
  makes on their behalf.

## Live activity log during agent phases

The job status screen shows a right-hand "Activity log" panel alongside the
pipeline stepper, updated live (via the existing status-polling loop) while an
agent-backed phase (`PLANNING`, `IMPLEMENTING`, `CORRECTING`) is running, and left
in place afterward so a failed job still shows what the agent was doing right up
to the failure — the primary motivation, since a bare `FAILED`/`stage` code alone
gives little sense of *what the agent had already tried* or *which file it was
touching* when it ran out of budget or hit an error.

- **Scope: tool calls only**, not free-text agent reasoning — a deliberate choice
  to keep the log terse, deterministic to render, and free of any risk of
  surfacing raw model chain-of-thought. Each entry is a short, human-readable
  summary of one tool call: `summarize_tool_use()` (`backend/app/steps/agent_progress.py`)
  maps a tool name + input dict to a line ("Reading `src/cli.py`", "Editing
  `src/cli.py`", "Searching for `"foo"` in `src/`", "Listing files matching
  `"*.py"`") for `Read`/`Write`/`Edit`/`Grep`/`Glob`; any other tool name (there
  is no `Bash` in this app's tool set, but the mapping is defensive) is silently
  skipped rather than shown raw.
- **Plumbing: a `ContextVar`, not a widened `JobSteps` signature.** Both
  `plan_agent.py` and `implement_agent.py`'s `execute_agent()` call
  `report(summarize_tool_use(...))` for each `ToolUseBlock` they see in an
  `AssistantMessage`, where `report()` looks up a sink function installed via the
  `report_progress_to(sink)` context manager. `runner.py` installs that sink
  around each `steps.generate_plan(...)` / `steps.implement_plan(...)` call so the
  agent modules stay decoupled from job persistence — they just call `report()`
  and don't know or care whether anything is listening. This was chosen over
  adding an `on_progress` parameter threaded through every `JobSteps` callable
  (which would mean touching every fake implementation across the test suite, as
  happened for `validation_failures` earlier) — empirically verified that a
  `ContextVar` set in the calling asyncio task survives the two nested
  `asyncio.to_thread` hops down to the SDK's own `asyncio.run(...)` call.
- **Reset per phase, capped at 50 entries.** `_start_activity_log()`
  (`app/jobs/runner.py`) clears `Job.activity_log` to `[]` at the start of each
  agent phase and returns a sink that appends + persists (via `store.save(job)`,
  safe to call from the agent's background thread — `JobStore` is
  `check_same_thread=False` behind its own lock) and truncates to the most recent
  50 entries. The log is deliberately **not cumulative across phases** — it always
  shows only the most recently run phase, which is what's relevant for diagnosing
  that phase's outcome; a job that fails during `IMPLEMENTING` doesn't show stale
  `PLANNING` entries mixed in.
- **Frontend:** `job-status-view.tsx` renders the panel only when there's
  something to show (log non-empty or the phase is currently active), titled
  present-tense ("Generating plan…" / "Applying code changes…") while `job.state`
  is genuinely live and past-tense ("Last agent activity") once the job has moved
  past that phase (including into a failed state) — derived purely from
  `job.state`, not `job.error?.stage`, so a job that failed *during*
  `IMPLEMENTING` still reads as "Last agent activity" rather than the live
  present-tense title. Entries render newest-first.

## Cost is recorded even when the job fails

A failed job never silently loses the Anthropic cost/tokens it already spent.
An agent call that *ran* — even one that ended in a typed failure
(`PLAN_INVALID`, `BUDGET_EXCEEDED`, `IMPLEMENTATION_INVALID`, a schema
validation failure, ...) — still burned real API cost, and that cost is worth
knowing when diagnosing why a job failed or budgeting spend.

- **`AppError` optionally carries `usage`** (`backend/app/core/errors.py`): a
  plain `dict[str, Any] | None` (an `AgentUsage.model_dump()`), not the
  `AgentUsage` model itself — `app.core.errors` is imported by
  `app.jobs.models`, so it can never import back from there without a cycle;
  the runner reconstructs `AgentUsage` from the dict when present.
- **Every raise site in `plan_agent.py` / `implement_agent.py` that has an
  `AgentRunOutcome` in scope attaches `usage=_usage_from(outcome).model_dump()`**
  — that covers a completed-but-unusable structured output, a harness
  budget/max-turns stop, an unexpected harness failure, and (using
  `last_outcome`) `PLAN_INVALID` after the single retry. The two genuinely
  outcome-less failures — a wall-clock timeout and a transport/exception
  before any `ResultMessage` arrives — leave `usage` unset, since there is
  nothing to report.
- **`runner.py` applies `err.usage` onto the job right before failing it**:
  `job.usage` in `run_job`, `job.implementation_usage` in `run_implementation`,
  `job.implementation_correction_usage` in `run_validation_correction` — a new
  field alongside `implementation_correction_result`/`_error`, since the
  correction pass previously dropped its own usage entirely, even on success.
  `run_implementation` additionally records `job.implementation_usage` as soon
  as `implement_plan` *returns*, before the "claimed changes never landed"
  consistency check can raise — that check's `AppError` is raised by the
  runner itself (no `outcome` in scope there), so without this the one
  implementation failure mode that isn't even an agent-side error would be
  the one case still missing its cost.
- **Frontend:** the "Cost summary" panel (`job-status-view.tsx`) is no longer
  gated on `job.plan` — a `PLANNING` failure never produces a plan but can
  still have spent cost, so gating on the plan would hide it entirely. It now
  renders whenever any of `job.plan` / `job.usage` / `job.implementation_usage`
  / `job.implementation_correction_usage` is present, and shows a "Correction
  cost" row alongside planning/implementation/total whenever a correction
  attempt recorded usage.

## Job history and admin cost reporting

`GET /jobs` lists jobs most-recent-first with keyset (`created_at`-cursor)
pagination, scoped to the requesting user's own jobs — an admin (or a request
made while `AUTH_ENABLED=false`, which has no ownership concept) sees every
job instead. The `jobs` table's `owner_user_id` column (added via an additive,
backfilling migration in `JobStore`, since older rows only had it inside the
JSON blob) makes this an indexed query rather than a full-table JSON scan.
The frontend surfaces this as a `/jobs` "My jobs" page.

`GET /admin/cost-summary` (admin-only; `FORBIDDEN` otherwise, including when
auth is disabled) aggregates `usage.total_cost_usd` and
`implementation_usage.total_cost_usd` per owner directly in SQLite via
`json_extract`/`SUM`, then resolves each `owner_user_id` to an email/display
name for display. Surfaced as an admin-only `/admin/costs` page, linked from
the header only when the signed-in user's role is `ADMIN`.

## Scope

**In:** non-secret form; a Jira ticket or an uploaded PDF requirement document as
alternative plan inputs; optional multi-user auth (dev login + trusted proxy);
delegated Jira/GitHub OAuth with encrypted-at-rest tokens (used for repository
discovery only — see below); async jobs with SQLite store + polling status
screen; the plan pipeline (fetch/clone/map/plan); an isolated-clone implement +
validate phase, available for local *and* remote repos alike; a single,
explicitly opt-in validation-correction pass after a failed validation; creating
a branch and committing the reviewed diff inside the isolated workspace, then
pushing it to the repo's real remote using ambient git auth (never force-pushes,
never opens a pull request); plan and diff review screens; a paginated
job-history list scoped to the owner (admins/no-auth-mode see all); an
admin-only per-user cost-usage dashboard; typed errors; quality gates;
security-invariant tests; a reference shared-deployment stack under `deploy/`.

**Out (not built; do not scaffold):** opening a pull request; delegated *git*
auth (clone and push both still use ambient credentials only — no per-user
token, including the already-connected GitHub OAuth token, is ever handed to a
git subprocess); the GitLab OAuth flow; a durable workflow engine; a
network-locked sandbox; an embeddings/vector index.

## Stack

- Frontend: Next.js (App Router) + React + TypeScript strict; eslint + prettier;
  Vitest. Talks only to the backend API; OAuth callback routes under `src/app/auth/`.
- Backend: FastAPI (Python 3.12+), jobs as in-process `asyncio` tasks, SQLite store
  (jobs, users, sessions, encrypted provider tokens); ruff + pyright; pytest.
- Repo map: tree-sitter via `tree-sitter-language-pack` (Python, TS/JS, Java, Go,
  C#, Rust; other files appear in the tree without symbols).
- Jira: Cloud REST API v3 — Basic auth (shared) or OAuth bearer (delegated).
- Requirement documents: `pypdf` for PDF text extraction; `python-multipart` for
  the job-create endpoint's multipart form/file handling.
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
