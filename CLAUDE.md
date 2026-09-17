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
- **GitHub and GitLab — delegated OAuth** (`GITHUB_OAUTH_*` / `GITLAB_OAUTH_*`):
  per-user tokens encrypted at rest, used **for repository discovery / quick-picks
  only**. `git clone` and `git push` both still inherit the machine's ambient git
  auth (SSH key / credential helper); delegated *git* auth is not built yet for
  either provider (see **GitLab OAuth integration** below for the full picture).
  **Each provider's tokens are encrypted under that provider's own Fernet key** —
  `encrypt_secret()`/`decrypt_secret()` (`backend/app/core/crypto.py`) both take a
  **required, no-default** `provider=` keyword argument ("jira", "github", or
  "gitlab", each resolving to its own `..._OAUTH_ENCRYPTION_KEY` via a small
  per-provider config table). A real bug shipped from `provider` having a default:
  `complete_github_authorization()` called `encrypt_secret()` with no `provider=`,
  silently encrypting under whatever the (then-)default was, while
  `list_github_repositories()` correctly decrypted with `provider="github"` —
  every GitHub connection completed while the two keys differed (the normal case)
  became permanently undecryptable, degrading to the same "Connect your GitHub
  account before loading repositories" message a never-connected user sees, even
  though the connect/callback step itself reported success. Caught by extending
  `test_auth_api.py`'s callback test to actually decrypt what got stored (it
  previously only checked "not plaintext"), plus a new end-to-end test that
  deliberately sets differing Jira/GitHub keys — the two existing repo-listing
  tests had bypassed the bug entirely by constructing their `RepoHostingConnection`
  fixtures with a correctly-provider'd `encrypt_secret()` call directly, never
  exercising the real callback code path. No store migration needed for
  connections saved before the fix: reconnecting overwrites the row
  (`save_repo_hosting_connection`'s `ON CONFLICT ... DO UPDATE`), so the existing
  "Disconnect" + "Connect" flow is already the fix. Fixed at the root rather than
  just at the one call site: `provider` was made a required keyword-only argument
  with no default at all, so a future provider forgetting to pass it is an
  immediate `TypeError` (caught by pyright/the first test run) instead of a
  silent wrong-key bug discovered only when decryption fails, weeks later.
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

## GitLab OAuth integration

Delegated per-user GitLab OAuth, mirroring the shape of the GitHub integration
(`backend/app/auth/gitlab_oauth.py`, `app/api/auth.py`'s `POST
/repo-hosting/gitlab/connect` / `GET /repo-hosting/gitlab/callback` / `GET
/repo-hosting/gitlab/repos`) — used for repository discovery/quick-picks only,
same as every other delegated provider in this app; `git clone`/`git push`
still never see a GitLab token.

- **Supports both gitlab.com and self-hosted instances**, not just gitlab.com:
  `settings.gitlab_instance_url` (default `https://gitlab.com`) is one
  configurable base URL used to build every GitLab endpoint the module calls
  (`/oauth/authorize`, `/oauth/token`, `/api/v4/user`, `/api/v4/projects`) —
  a self-hosted deployment just points this at its own instance, with no code
  change. The URL is normalized to strip any trailing slash (a
  `field_validator` on `Settings.gitlab_instance_url`), since every call site
  builds paths as an f-string (`f"{gitlab_instance_url}/oauth/token"`) and a
  trailing slash in the configured value would silently double it.
- **Outbound HTTPS calls trust the OS certificate store, not just `certifi`.**
  A self-hosted instance behind a corporate network (this app's own dev/test
  case: `gitlab.dsslp.com`, issued by an internal Active Directory CA) is
  trusted by Windows — and therefore by a browser completing the OAuth
  redirect — but `httpx`'s default `ssl` context only trusts the public roots
  bundled in `certifi`, so the backend's own token-exchange/user-lookup calls
  failed with `CERTIFICATE_VERIFY_FAILED` even though the browser leg of the
  same flow worked fine. `truststore.inject_into_ssl()` (`backend/app/main.py`,
  called before any other import that could construct an `ssl.SSLContext`)
  swaps in the OS-native trust store globally for every `httpx` call in the
  process — not a GitLab-specific patch, so it also covers Jira/GitHub/GitLab
  OAuth and the Anthropic SDK's own HTTP calls against any future
  internally-hosted or corporately-proxied endpoint.
  **Symptom this produces if the underlying SSL failure isn't the first thing
  checked:** the OAuth state row is consumed (deleted) by
  `consume_provider_oauth_state()` *before* the token exchange runs, so the
  callback fails with a generic `REPO_PROVIDER_CALLBACK_FAILED` — but React
  StrictMode's double-invoked effect (`gitlab-callback-page.tsx`, dev-mode
  only) fires the callback request twice with the same code/state; the first
  (real) attempt's response is discarded because the component's cleanup
  already set `active = false`, and the *second*, redundant attempt is what
  renders — and since the state was already consumed by attempt one, it
  reports `REPO_PROVIDER_STATE_INVALID` ("sign-in attempt is missing or
  expired") instead of the actual SSL error, which is only visible in the
  backend log. Diagnose from the backend log, not the browser-visible message,
  the same way `error_max_structured_output_retries` failures are diagnosed
  above.
- **Token refresh, unlike GitHub.** This app's GitHub OAuth App config issues
  long-lived tokens with nothing to refresh, but GitLab access tokens expire
  in ~2 hours and rotate a refresh token on each use. `_is_token_stale()` /
  `_get_valid_access_token()` / `_refresh_connection()` model this on
  `jira_oauth.py`'s existing stale-token/refresh pattern (Jira access tokens
  have the same short-lived-plus-refresh shape) rather than GitHub's
  simpler no-refresh module, since GitLab's token lifecycle is the closer
  match.
- **Crypto hardening landed alongside this feature, not after it.** Building
  a third provider onto `crypto.py` was the trigger for removing its unsafe
  `provider: str = "jira"` default (see the GitHub encryption-key bug above)
  — `encrypt_secret()`/`decrypt_secret()` now require `provider=` as an
  explicit keyword with no default, so a GitLab call site that forgot to pass
  `provider="gitlab"` fails immediately (`TypeError`) instead of silently
  encrypting under the Jira key the way the GitHub bug did. `_PROVIDERS`, a
  `dict[str, _ProviderCryptoConfig]` keyed by provider name (`"jira"` /
  `"github"` / `"gitlab"`, each with its own key-getter, error codes, and
  label), replaced the old if/elif chain so adding a fourth provider later is
  a dict entry, not a new branch to remember.
- **No store or model changes needed.** `RepoHostingConnection`,
  `RepoHostingConnectionInfo`, and the `store.*_repo_hosting_connection`
  functions were already provider-agnostic (a `provider` column/field, not a
  GitHub-specific shape), and the generic `DELETE /repo-hosting/{provider}`
  route already worked for any provider string. Only
  `GitLabRepositorySummary`/`GitLabRepositoryListResponse` (`app/auth/models.py`)
  were new, for GitLab API v4's own repo-list response shape (`path_with_namespace`,
  `http_url_to_repo`, etc., distinct from GitHub's field names).
- **No `disconnect_gitlab_connection()` was added, deliberately.** Reading
  `github_oauth.py` for this feature surfaced that its own
  `disconnect_github_connection()` is dead code — the real disconnect route
  uses the generic `disconnect_repo_hosting_connection`
  (`app/auth/repo_hosting.py`) instead. Replicating the same redundancy for
  GitLab would just be more unused code to maintain.
- **Frontend:** `startGitLabConnect()` / `completeGitLabConnect()` /
  `fetchGitLabRepositories()` (`frontend/src/lib/api.ts`); a dedicated
  callback page (`gitlab-callback-page.tsx`, routed at
  `src/app/auth/gitlab/callback/page.tsx`, mirroring the GitHub callback
  page); `auth-gate.tsx`'s `handleRepoProviderConnect` gained a `GITLAB`
  branch plus flash-message/URL-cleanup handling for the GitLab redirect.
  `job-form.tsx` fetches GitLab repos alongside GitHub ones and lists
  "Connected GitLab repos" as quick-picks the same way; fixed in the same
  change — the GitHub-fetch failure path used to `return` early out of the
  loading effect, which would have skipped the GitLab fetch entirely once it
  was added right after; it now falls through instead.
- **Tests:** 9 new backend tests (`test_auth_api.py`) covering connect/callback/repos
  happy paths, token-refresh-on-stale-token, and the self-hosted-instance-URL
  case; 2 new frontend tests for the callback page
  (`gitlab-callback-page.test.tsx`) plus updated `auth-gate.test.tsx` /
  `job-form.test.tsx` coverage for the connect flow and quick-picks.

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
`CLAUDE.md`/`AGENTS.md`/`SKILLS.md`/`README.md` (`_REPO_DOC_NAMES` in
`plan_agent.py`, first one present wins, capped by `AGENT_REPO_DOC_MAX_CHARS`) so
it needs fewer exploration reads — this is planning-only: the implementation
step (`implement_agent.py`) doesn't front-load any doc file, though its
`Read`/`Grep`/`Glob` tools can still surface one incidentally during its own
exploration of the clone; and a **deterministic repo digest**
(`backend/app/steps/repo_digest.py`, zero tokens) — languages, top-level layout,
key files, core modules by symbol count, README excerpt — is computed once per
repo state, cached (`AGENT_REPO_DIGEST_*`), and injected into every plan so the
agent orients without exploring. Context management/compaction is handled by the
harness.

**Diagnosing and avoiding `BUDGET_EXCEEDED`:** it's one error code for three
different underlying limits — turns, cost, and wall-clock (see Failure
behavior below) — so before raising any cap, check the failed job's recorded
`usage` (turns/cost/duration — now populated even on failure, see **Cost is
recorded even when the job fails**) against the three `AGENT_PLAN_*` config
values to see which one actually tripped, and read the job's `activity_log`
(**Live activity log during agent phases**) to see what the agent was doing
right before it stopped:

- A **converging** search (progressively narrowing toward one area) that
  simply ran long → raising the relevant cap is the right call.
- A **wide, unconverged** search bouncing between unrelated files/modules →
  the ticket needs a map, not more budget. Two levers, in priority order:
  1. **Ticket-specific `planning_notes`** naming the actual relevant files —
     free, immediate, and the single highest-leverage fix since it skips
     exploration entirely instead of paying to re-discover it.
  2. **A repo-root `CLAUDE.md`/`AGENTS.md`/`README`** (front-loaded per
     token-saving controls above) — zero-token, but repo-wide, so it helps
     every ticket touching that area a little rather than one ticket a lot;
     it won't fully compensate for a ticket that genuinely spans several
     modules a generic doc can't enumerate in advance.
  3. Only after both of the above: raise the cap as a stopgap. Caps live in
     `backend/.env` (`AGENT_PLAN_MAX_TURNS`, `AGENT_PLAN_MAX_BUDGET_USD`,
     `AGENT_TIMEOUT_SECONDS`); raising one just pays more to search just as
     blindly if the real cause is #1/#2.
  4. If a ticket genuinely requires coordinated cross-module changes, consider
     whether it should be split — the plan's own `complexity_level` /
     `estimated_story_points` fields exist to flag exactly this.
- The plan cache only stores *successful* plans, so a ticket that has already
  failed with `BUDGET_EXCEEDED` gets no cache discount on retry — every
  attempt (including "just testing whether it works now") costs full price
  until one succeeds.

**Diagnosing `error_max_structured_output_retries`** (surfaced as
`PLAN_INVALID` after the single retry, or `IMPLEMENTATION_INVALID` with the
"could not produce a properly formatted result" message): a *different*
failure from `BUDGET_EXCEEDED` — the harness gave up trying to get the
model's final answer to validate against the plan/implementation schema, not
a turns/cost/timeout cap. All caps can be well within budget when this
happens. `AgentRunOutcome.errors` (both `plan_agent.py` and
`implement_agent.py`) is typically just a terse summary ("Failed to provide
valid structured output after 5 attempts") — not enough to tell *why*
validation kept failing. `_describe_structured_output_failure()` (in each
module) additionally captures `AgentRunOutcome.result` (the harness's own
natural-language remark, when reported) and `structured_output` (the model's
last, invalid attempt, when the SDK still populates it on this subtype) into
`internal_detail`, each capped at `_FAILURE_DETAIL_MAX_CHARS` (2000) so one
huge field can't crowd the others out of the log line — logged (redacted) but
never returned to the client, same as every other `internal_detail`. Neither
field is guaranteed to be populated by the harness on every failure, but when
they are, they're usually far more diagnostic than the summary alone. Check
the backend log for the job in question rather than guessing at a cause from
the generic user-facing message.

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

A document-sourced job's ticket key (`RequirementSource.DOCUMENT`) is resolved
in priority order by `_resolve_document_ticket_key()` (`app/api/routes.py`),
since most requirement PDFs are themselves exported from a Jira ticket and it's
worth naming/branching the job the same way a truly Jira-sourced job would be,
rather than always showing an opaque generated id:

1. **An explicitly typed/pasted key**, submitted via the optional
   `document_ticket_key` form field next to the PDF upload (validated by
   `normalize_document_ticket_key()` — the same bare-key-or-URL parsing and
   credential-shape rejection as the Jira ticket field itself).
2. **Auto-detected from the PDF's own text** (`detect_ticket_key()` in
   `app/steps/document_fetch.py`): a best-effort, timeout-guarded regex scan of
   just the first ~500 chars extracted from the upload (Jira's own "Export to
   PDF" puts the issue key in the document's title/header) — deliberately not
   the whole document body, to avoid picking up an unrelated key mentioned
   later in the description or a comment.
3. **A deterministic hash fallback**, `DOC-` + the first 8 hex characters of a
   SHA-256 hash of the uploaded PDF's raw bytes — used only when neither of the
   above resolves. Matched by `_SYNTHETIC_DOCUMENT_KEY_RE` in `repo_clone.py`
   as a full pattern (`DOC-` + exactly 8 uppercase hex chars), not a bare
   prefix, so a real Jira project abbreviated "DOC" (ticket key `DOC-31`) is
   never mistaken for it.

This resolution runs synchronously in the request handler, before the PDF is
even saved to disk — the auto-detect step does its own lightweight, capped,
best-effort text extraction (`extract_pdf_text_from_bytes`) purely to search
for a key; it is not the authoritative extraction (that's still
`fetch_requirement_document`, run later in the background job pipeline with
the full `DOCUMENT_MAX_EXTRACTED_CHARS` budget), and any failure here (a
malformed PDF, a timeout) just means falling through to the hash, never a
failed job submission.

**Why this matters beyond naming:** the planning prompt embeds `ticket.key`
(`plan_agent.build_prompt`), and the plan cache (`AGENT_PLAN_CACHE_ENABLED`)
memoizes on a hash of the *full rendered prompt* — so whichever key gets
resolved must be deterministic across resubmissions, or every PDF-sourced job
is a guaranteed cache miss even when nothing about the request actually
changed (this was a real, reported bug before the hash fallback was made
deterministic — a random `uuid4()` key per upload, the original behavior,
defeated the cache on every single PDF-sourced job). Re-uploading the exact
same PDF bytes still reproduces the same key at every priority level and hits
the cache; a PDF with different bytes — even one that resolves to visibly
identical text after extraction (e.g. re-exported through different
PDF-generation software) with no explicit field and no auto-detected key —
only cache-hits if it happens to still auto-detect to the same key, since the
hash fallback runs on raw bytes, not extracted text.

**Branch naming and the local-branch-match check both already key off
`Job.requirement_source`, not the ticket key's shape, so a real resolved key
needed no changes there:** `default_branch_name` (`branch_prep.py`) still
appends a slugified plan summary for every `DOCUMENT`-sourced job regardless
of what the key looks like (`jira2pullreq/kan-31-<slug>` reads better than the
bare `jira2pullreq/<hash>` a synthetic key would produce, but both are
handled identically by that function); and `REQUIRE_LOCAL_BRANCH_TICKET_MATCH`
is correctly still skipped only for the hash-fallback case (via
`_SYNTHETIC_DOCUMENT_KEY_RE`) — a document job resolved to a real key is
correctly branch-matched just like a genuine Jira-sourced job would be, since
in that case the key really could (and, if the PDF was exported from that
ticket and the developer is working the same ticket locally, likely does)
appear in the branch name.

Text extraction (`backend/app/steps/document_fetch.py`, `pypdf`) is
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
Retry redirect; the job form shows which filename to re-upload). The optional
typed ticket key *is* carried forward in the retry draft — but only when the
failed job actually resolved to a real key rather than the hash fallback
(`SYNTHETIC_DOCUMENT_KEY_RE` in `job-status-view.tsx`, mirroring the backend's
own check), so the user isn't asked to retype something they already provided
or the app already detected, while a hash key (meaningless to a human) is
correctly left for auto-detection or the fallback to resolve again.

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

**Normalizing CRLF line endings around the implementation agent.** The
implementation agent's `Edit` tool needs a byte-exact match between the
`old_string` it composes and what's actually on disk. A repo that stores CRLF
line endings (common for a Windows-developed codebase with no
`.gitattributes` text-normalization rule) can make every multi-line edit
silently fail to apply — diagnosed from a real job's recorded activity log
and usage: ~15 `Edit` calls repeated across the same handful of files, zero
net diff in the workspace afterward, and a garbled final structured summary.
`app/steps/line_endings.py`'s `normalize_to_lf()` runs right before each
`implement_plan` call in `run_implementation`/`run_validation_correction`
(never around planning, which only reads/greps), converting every CRLF text
file in the workspace to LF in place and recording which paths it touched;
`restore_original_line_endings()` runs in a `finally` block immediately
after — including on a raised `AppError`, so a diff collected afterward (the
happy path, or `_refresh_diff_best_effort` on a failure) never shows
line-ending-only noise — converting exactly those paths back to CRLF,
*including* any new content the agent wrote into them, so the file's
original convention survives into the diff, branch, and commit unchanged.
Binary detection mirrors git's own heuristic (a NUL byte in the first 8KB);
`.git`, common vendor/build directories, and files over
`WORKSPACE_LINE_ENDING_MAX_FILE_BYTES` are skipped untouched. On an LF-only
repo the whole mechanism is a no-op (nothing to convert), so
`WORKSPACE_NORMALIZE_LINE_ENDINGS` defaults to on. Known limitation: a file
with genuinely *mixed* CRLF and bare-LF endings loses that per-line
distinction on restore (every line comes back as CRLF) — accepted as a small
amount of diff noise on an already-inconsistent file, in exchange for keeping
the mechanism simple; a consistently-endian file (the common case) round-trips
exactly.

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
  `jira2pullreq/<ticket-key>-<slugified-plan-summary>` for a document-sourced job
  (`default_branch_name`) — always with the summary slug appended, since the
  key alone means nothing to a reviewer when it's the hash fallback (see
  **Requirement source** above for when a document job's key is a real Jira
  key vs. that fallback). Both the branch name and the commit message (default:
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

**Approving implementation scrolls focus back to the status panel.** A plan
review can be long (impacted files, proposed changes, risks, open questions),
so "Approve and Implement" is often clicked from far down the page — without
this, the pipeline stepper the user actually wants to watch next is
off-screen above, and they'd have to scroll back up themselves to see
anything change. `focusStatusPanel()` (`job-status-view.tsx`) calls
`scrollIntoView({ behavior: "smooth", block: "start" })` then
`.focus({ preventScroll: true })` on the status-header section (`tabIndex={-1}`,
so it's programmatically focusable without joining the tab order) at the
start of `handleImplement()`, before the `implementJob()` call — the move
happens immediately on click, not after the network round-trip. `preventScroll`
stops the browser's own focus-triggered jump from fighting the smooth scroll.
Styled with a plain `:focus` (not `:focus-visible`) outline so the cue always
shows for this deliberate, script-triggered focus move, regardless of the
browser's keyboard-vs-pointer heuristic. (Test-environment note: jsdom doesn't
implement `scrollIntoView` at all — `src/test/setup.ts` polyfills a no-op so
tests that click this button don't throw; real browsers have always had it.)

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
delegated Jira/GitHub/GitLab OAuth with encrypted-at-rest tokens (used for
repository discovery only — see below); async jobs with SQLite store + polling
status screen; the plan pipeline (fetch/clone/map/plan); an isolated-clone
implement + validate phase, available for local *and* remote repos alike; a
single, explicitly opt-in validation-correction pass after a failed validation;
creating a branch and committing the reviewed diff inside the isolated
workspace, then pushing it to the repo's real remote using ambient git auth
(never force-pushes, never opens a pull request); plan and diff review screens;
a paginated job-history list scoped to the owner (admins/no-auth-mode see all);
an admin-only per-user cost-usage dashboard; typed errors; quality gates;
security-invariant tests; a reference shared-deployment stack under `deploy/`.

**Out (not built; do not scaffold):** opening a pull request; delegated *git*
auth (clone and push both still use ambient credentials only — no per-user
token, including the already-connected GitHub/GitLab OAuth token, is ever
handed to a git subprocess); a durable workflow engine; a network-locked
sandbox; an embeddings/vector index.

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
- TLS trust: `truststore` (injected in `app/main.py`) makes every outbound
  `httpx` call use the OS certificate store instead of `certifi`'s public-only
  bundle — required for any self-hosted/internal provider instance whose
  certificate chains to an internal CA (see **GitLab OAuth integration**).
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
