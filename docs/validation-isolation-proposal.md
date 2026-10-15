# Proposal: isolate the validation step from the backend's secrets

**Status:** signed off by the owner (2026-10-15). **Phase 0 and phase 1 are
implemented** (see §10 for what was built and where it differs from the text
below). Phase 2 is approved in principle with rootless Docker + gVisor as the
runtime, and not built yet.

**Author:** Claude Code session (validation-isolation-proposal), 2026-10-13.

## Owner decisions (2026-10-15)

| # | Question (§8) | Decision |
|---|---|---|
| 1 | Phase 0 now | **Approved.** Done |
| 2 | Phase 1, including `PR_SET_DUMPABLE` | **Approved.** Done |
| 3 | Phase 2 runtime | **Rootless sandbox daemon + gVisor.** Not built yet |
| 4 | Q1, `npm --ignore-scripts` | **Run them.** Lifecycle scripts stay on; no setting added |
| 5 | Invariant 6 and its two checks | **Approved as written.** Added to CLAUDE.md |
| 6 | F1 (git hooks / `.git` writes) | Being fixed in a separate change, not here |

## TL;DR

The post-implementation validation step runs the target repo's own code
(`npm ci`/`npm install` lifecycle scripts, `npm run lint`, `npm test`,
`pytest`) inside the backend process's security context. That code can read
every server secret, the whole SQLite database, and other users' workspaces,
and it can open network connections. Whatever it prints is stored unredacted,
shown on the job page, and sent to Anthropic in the correction prompt. For a
shared UAT/production VM serving EU clients this is the main blocker.

Recommended order:

| Phase | What | Effort | Closes |
|---|---|---|---|
| **0 (now) — done** | Turn validation off on shared deployments until phase 2 lands (new `VALIDATION_ENABLED`, forced to `false` in `deploy/vm/docker-compose.yml`) | ~0.5 day | Everything, on the VM, at the cost of a feature that **doesn't work there today anyway** (§2.5) |
| **1 (stopgap) — done** | Allowlisted child environment, per-run `HOME`, `python -I` for ruff, redaction of `output_excerpt`, validation on a `.git`-less copy of the workspace, eager secret registration | 2–3 days | Accidental and casual leaks: secrets in env, secrets printed into output, validation tampering with the workspace's `.git` |
| **2 (target)** | Validation in a throwaway container per run, launched by a small broker sidecar: no secrets, no DB, no other workspaces, CPU/memory/PID/time limits, network limited to a registry proxy during install and none afterwards | 7–10 days (+1–2 for rootless/gVisor) | A deliberate attacker who controls the repo or steers the implementation agent |

Phase 1 alone is **not** a security boundary: in the container the child can
still read `/proc/1/environ` (the backend's original environment), and on a
developer machine it can read `backend/.env` from disk (§3.4). It is worth
doing because it is cheap, it is the right behaviour for local dev
permanently, and phase 2 reuses all of it. But the VM should not run
validation until phase 2 exists, hence phase 0.

---

## 1. Problem, as confirmed in code

### 1.1 What runs

`validate_workspace()` (`backend/app/steps/validation_runner.py:232`)
auto-detects a profile and calls `_run_command_sync()`, which runs
`subprocess.run(argv, cwd=workspace_path, capture_output=True, ...)`
(`validation_runner.py:185`) **with no `env=`**. The child inherits the
backend's full environment.

| Command | Whose code executes |
|---|---|
| `npm ci` / `npm install` | `preinstall`/`install`/`postinstall`/`prepare` scripts of the repo **and of every transitive dependency** |
| `npm run lint`, `npm test` | Arbitrary shell from the repo's `package.json`, plus test files |
| `python -m pytest -q` | The repo's `conftest.py`, test files, and anything they import |
| `python -m ruff check .` | Intended to be static only, but see §1.4: a `ruff.py` in the repo root shadows ruff and runs instead |

All of it runs in the target repo's workspace, which the implementation agent
has just written to. So there are two independent sources of hostile code:

1. **The repository itself** (or a compromised npm dependency): a malicious
   or compromised client repo, or a supply-chain `postinstall`.
2. **The implementation agent, steered by prompt injection.** Ticket text,
   attached PDFs, uploaded requirement documents, and repo files are untrusted
   data (invariant 5). An injected instruction like "add a test that prints
   the environment for debugging" yields a test file that does exactly that.
   The workspace guard confines *where the agent writes*, not *what the
   written code does when validation later runs it*.

A third actor matters on a multi-user deployment: **a legitimate user**
pointing a job at a repo they control, to harvest server secrets or other
users' data.

### 1.2 What the child can reach today

| Asset | How | Impact |
|---|---|---|
| `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA/GITHUB/GITLAB/BITBUCKET_OAUTH_CLIENT_SECRET` | `os.environ` (VM: `env_file:` in compose puts every `.env` value in the process environment) | Spend on our Anthropic account; impersonate our OAuth apps |
| `*_OAUTH_ENCRYPTION_KEY` (4 Fernet keys) | Same | Combined with the DB (next row): decrypt **every user's** Jira/GitHub/GitLab/Bitbucket token, including write-scoped push tokens |
| `var/jobs.db` | Plain file read, same uid (VM: `/app/var/jobs.db`; dev: `backend/var/jobs.db`; from the workspace `var/workdir/<job>/repo` that is just `../../../jobs.db`) | Encrypted provider tokens; **plaintext session IDs** (`sessions.id` is the cookie value), so any signed-in user can take over another user's, or an admin's, app session; every job's ticket text, plan, and diff |
| Other jobs' workspaces | `var/workdir/<other-job>/repo` | **Other clients' source code.** This is the EU-sensitivity problem in its plainest form |
| Uploaded requirement PDFs | `var/uploads/<job>/requirement.pdf` | Other clients' requirement documents |
| Network | Unrestricted egress from the backend container/host | Exfiltrate all of the above; reach the cloud metadata endpoint (`169.254.169.254`) if the VM has a managed identity |
| The workspace's own `.git` | Write access | §1.5: plant a hook that runs later inside the backend, outside validation entirely |

### 1.3 Where the output goes

`output_excerpt` is the first 1,200 characters of combined stdout+stderr
(`validation_runner.py:151-157`, `:209`, `:221`, `:228`), **not passed
through `redact()`**. It is:

- persisted in the job record (SQLite JSON blob);
- returned by `GET /jobs/{id}` and rendered on the job page;
- quoted into the correction agent's prompt
  (`implement_agent._validation_failures_block`, `implement_agent.py:120-134`),
  so it is sent to Anthropic.

So the simplest exploit needs no network at all: a test that does
`print(os.environ)` and fails. The secret appears on the job page.

(The `logger.info` call at `:183` logs only the static command and goes
through the redacting log filter, so logs are fine.)

### 1.4 Module shadowing in the Python profile

Both Python commands are `[sys.executable, "-m", <tool>, ...]` with
`cwd=workspace`. `python -m` puts the current directory first on `sys.path`,
so a `ruff.py` (or `ruff/__main__.py`) in the repo root is imported **instead
of** ruff. This turns the one validation step that should only parse code
into an arbitrary code execution step, and it runs in the **backend's own
virtualenv**, where `import app.core.secrets` may resolve. Relevant even on
the VM image, where real ruff isn't installed (§2.5): the shadow module is
found first regardless.

### 1.5 Related: validation can plant code that runs later, inside the backend

Validation runs **in place** in `var/workdir/<job>/repo`, `.git` included.
Code running during validation (a `postinstall` script, or husky's
`prepare`, which sets `core.hooksPath` legitimately) can write
`.git/hooks/pre-commit`, `.git/hooks/pre-push`, `core.hooksPath`,
`core.fsmonitor`, or a `filter.<x>.clean` driver into `.git/config`. Later:

- **Create branch** (`branch_prep.py:162-172`) runs `git checkout -B`,
  `git add -A` and `git commit` with no `--no-verify` and, for the plain
  calls, the backend's full environment (`env=None`; with author config,
  `_NON_INTERACTIVE_ENV = {**os.environ, ...}`, line 49). The hook runs
  inside the backend with every secret.
- **Push** runs with `-c http.extraHeader=Authorization: Basic …`. Git
  exports `-c` values to child processes in `GIT_CONFIG_PARAMETERS`, so a
  `pre-push` hook can read **the clicking user's delegated write token**,
  and with an admin clicking Push, the admin's.

Validation side effects (coverage reports, snapshot updates, build output
not covered by `.gitignore`) also currently end up in the commit via
`git add -A`, after the user reviewed the diff. Phase 1's "validate on a
copy" (§3.3) closes the validation half of this. The **implementation agent**
can write `.git/hooks/*` directly too (`workspace_guard.py` allows any path
inside the workspace, `.git` included). That is outside this proposal's
scope and is listed as follow-up F1 (§7).

### 1.6 What this proposal does not claim

These findings come from reading the code. No exploit was run against a live
deployment. The tests in §6 are designed to demonstrate each one first,
failing on today's code, before any fix lands, the same way earlier fixes in
this repo were verified.

---

## 2. Goals, non-goals, constraints

### 2.1 Goals

1. **No server secret is reachable** from code run during validation: not via
   env, files, `/proc`, process memory, or network to the backend.
2. **No cross-job data is reachable:** no DB, no other workspaces, no uploads.
3. **No secret reaches `output_excerpt`**, even if the repo's own code tries
   to print one (defence in depth for goal 1, and for secrets committed
   inside the client's own repo).
4. **Validation cannot change what gets committed or pushed.**
5. **Bounded resources:** a fork bomb, memory hog, disk filler or infinite
   loop fails that validation step, not the VM.
6. **Restricted network:** package registry only during install, nothing
   afterwards.
7. Local Windows development keeps working without Docker.

### 2.2 Non-goals

- Sandboxing the planning/implementation **agents**. They don't execute repo
  code (no `Bash` tool); they are confined by `workspace_guard.py`.
- Installing the target repo's **Python** dependencies (pytest today runs in
  the backend's venv, where the repo's imports mostly fail). Phase 2 makes it
  possible (§4.6) but doesn't include it.
- A general CI system. Profiles stay auto-detected and fixed.

### 2.3 Constraint: the validation contract doesn't change

`validate_workspace(path) -> list[ValidationResult]`, `INSTALL_STEP_NAME`,
`correctable_failures()` and the `ValidationResult` fields stay as they are,
so `runner.py`, the correction flow, the frontend and every fake-step test
are untouched. Only the execution backend behind `_run_command_sync` changes.

### 2.4 Constraint: secrets architecture

The app reads secrets lazily (`core/secrets.py`) and holds them in the
backend process. Nothing here changes that. Moving secrets into a separate
service is a bigger redesign; phase 2 achieves the goals by moving the
**untrusted code** out instead.

### 2.5 Finding: validation on the VM doesn't work today

`backend/Dockerfile`'s runner stage installs only `git` and `ca-certificates`
plus the venv from `uv sync --no-dev`. `pyproject.toml` lists `ruff` and
`pytest` under `[dependency-groups] dev`, which `--no-dev` excludes, and there
is no Node.js. So on the VM:

- `npm lint`/`npm test`/`npm install` → `SKIPPED` ("Tool is not available").
- `ruff`/`pytest` → `_resolve_executable(sys.executable)` succeeds, so they
  run and **fail** with "No module named ruff/pytest". The job page shows a
  validation failure that isn't a code defect, and offers "Attempt automatic
  fix" for it.

This is why phase 0 costs nothing: validation on the VM is already broken,
and the one thing it reliably does there is run a shadow `ruff.py`/`pytest.py`
if a repo ships one (§1.4). Phase 2's validation image (node + python + ruff +
pytest) fixes the functionality and the isolation together.

---

## 3. Phase 0 and phase 1: stopgap

### 3.0 Phase 0: off switch for shared deployments

- New setting `VALIDATION_ENABLED` (default `true`, so local dev is
  unchanged). When `false`, `validate_workspace` returns one `SKIPPED`
  result: "Validation is disabled on this deployment." No command runs.
- `deploy/vm/docker-compose.yml` sets `VALIDATION_ENABLED: "false"` under
  `environment:`, next to the forced `AUTH_*` values, so a stray `.env`
  value can't turn it back on.
- When phase 2 ships, the compose value becomes
  `VALIDATION_SANDBOX: docker` and the off switch stays available.

The rest of the pipeline (plan, implement, diff, branch, push) is
unaffected. `correctable_failures()` returns nothing for `SKIPPED` results,
so the correction card simply doesn't appear.

### 3.1 Allowlisted environment

Replace the inherited environment with one built from an **allowlist**, not
a denylist. The existing `scrubbed_env()` in `plan_agent.py`/`implement_agent.py`
is a denylist (5 names plus registered values), and registration is lazy, so
a secret the process hasn't read yet, such as `GITLAB_OAUTH_CLIENT_SECRET`
before anyone connects GitLab after a restart, is not filtered. For code we
know is hostile, only an allowlist is safe.

```python
# validation_runner.py (sketch)
_PASSTHROUGH = (
    # Required to find tools and run them at all.
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    # Windows: Python/Node fail without these; .cmd shims need COMSPEC/PATHEXT.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # Corporate TLS (the Dockerfile sets SSL_CERT_FILE for the internal CA).
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")

def _validation_env(run_home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    # A proxy URL may carry credentials (http://user:pass@proxy): pass it
    # only when it has no userinfo.
    env |= {k: v for k in _PROXY_VARS if (v := os.environ.get(k)) and "@" not in v}
    tmp = run_home / "tmp"
    env |= {
        # Per-run home: no ~/.npmrc (registry tokens), ~/.git-credentials,
        # ~/.config/gh, ~/.docker/config.json, ~/.claude of the backend user.
        "HOME": str(run_home), "USERPROFILE": str(run_home),
        "APPDATA": str(run_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(run_home / "AppData" / "Local"),
        "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "npm_config_cache": str(run_home / "npm-cache"),
        "npm_config_userconfig": str(run_home / ".npmrc"),  # empty file
        "npm_config_globalconfig": str(run_home / "npmrc-global"),  # empty file
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "CI": "true",            # test runners: no watch mode, no prompts
        "NO_COLOR": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
    }
    _assert_no_secret_values(env)   # see below
    return env
```

`_assert_no_secret_values` is a belt-and-braces check run on every call:
it raises `AppError(INTERNAL)`, so validation fails closed, if any value
equals a registered secret or any key matches
`(?i)(token|secret|key|password|credential)`. It catches a future edit that
adds the wrong variable to the allowlist.

`run_home` is a fresh directory per validation run, under
`var/workdir/<job>/validation-home/`. It is outside the repo copy (§3.3) and
deleted afterwards. The npm cache is per-run, so nothing is shared between
jobs or tenants. A shared cache would be faster, but without a lockfile
(`npm install`) cache entries aren't integrity-pinned, so one tenant's run
could poison another's. Phase 2 replaces the cache with a registry proxy
(§4.4).

**Open question for the owner, Q1:** `npm ci --ignore-scripts`. It removes
the install-time attack surface from transitive dependencies, but breaks
packages that build or download binaries in `postinstall`, and husky. The
proposal is a `VALIDATION_NPM_IGNORE_SCRIPTS` setting, default `false` in
phase 1. Phase 2 contains install scripts instead of forbidding them, so it
matters less there.

### 3.2 Redact the output

In `_run_command_sync`, both the normal and the timeout paths:

```python
output_excerpt=_truncate_output(redact(combined_output))
```

Redact **before** truncating, so a secret that straddles the 1,200-character
cut can't leak half of itself in a form the redactor no longer matches.

`redact()` only knows *registered* secrets plus token patterns, and
registration is lazy. Add `secrets.register_configured_secrets()`, called
once at startup in `app/main.py`. It calls every `get_*` reader in
`core/secrets.py`, so each configured value is in the registry before any
validation runs. That is the same "registered" definition invariant 2
already relies on, made eager.

Redaction is a **backstop, not a boundary**. Code that base64-encodes,
reverses, or splits a secret defeats it, and network exfiltration bypasses
output entirely. It stays in phase 2 regardless, because a client's repo may
contain its own committed credentials, which the token patterns catch.

### 3.3 Validate on a copy of the working tree, without `.git`

Instead of running in `var/workdir/<job>/repo`:

1. Sync the working tree into `var/workdir/<job>/validation/`, excluding
   `.git` and `node_modules`. Keep that directory across validation runs of
   the same job, so the post-correction revalidation reuses its
   `node_modules` as it does today (`npm_install_command` already skips when
   `node_modules` exists).
2. Run the commands there.
3. Copy **nothing** back.

This gives goal 4 (validation can't touch `.git`, so it can't plant hooks,
and its artifacts never reach `git add -A`), and it is the same "tree in,
results out" contract phase 2 needs, so phase 2 only swaps the executor.
The copy reuses the exclusion logic `repo_clone.py` already has for
`LOCAL_FOLDER` sources. Cost: one tree copy per validation run, small next to
`npm ci`. Tools that need git metadata (husky, lint-staged,
`git describe`-based versioning) see "not a git repository". Usually
harmless in a validation run; noted in §5.

### 3.4 `python -I` for ruff

Run ruff as `[sys.executable, "-I", "-m", "ruff", ...]`. `-I` (isolated
mode) drops the current directory from `sys.path` and ignores `PYTHON*`
variables and user site-packages, so a `ruff.py` in the repo can't replace
ruff. Don't apply it to pytest: projects rely on `python -m pytest` adding
the root to `sys.path`, and pytest runs the repo's code by design anyway.
Containing pytest is phase 2's job.

### 3.5 What phase 1 does not fix

| Residual risk | Why | Fixed by |
|---|---|---|
| **VM:** `cat /proc/1/environ` | uvicorn is PID 1 in the container, running as the same `appuser` uid as the child, and `/proc/<pid>/environ` holds the *original* environment block, which `env=` doesn't touch | Phase 2 (different container). A cheap partial: `prctl(PR_SET_DUMPABLE, 0)` at backend startup makes `/proc/<pid>/*` root-owned and stops same-uid `ptrace` and `/proc/<pid>/mem` reads; recommended as part of phase 1 on Linux |
| **Dev:** read `backend/.env` from disk | The workspace lives inside the checkout, so `backend/.env` is `../../../../.env` relative to `backend/var/workdir/<job>/validation`. Pydantic and `secrets._read` both read that file directly | Not fixed locally. Accepted: on a developer machine, the developer's own secrets and the developer chose the repo. Phase 2 via Docker Desktop is optional for those who want it |
| `var/jobs.db`, other workspaces, uploads | Same uid, plain file reads | Phase 2 |
| Network exfiltration | No egress control in a plain subprocess | Phase 2 |
| Resource exhaustion | Only a wall-clock timeout today; a fork bomb or memory hog takes the backend down with it | Phase 2 |

---

## 4. Phase 2: target design, a throwaway validation sandbox

### 4.1 Shape

```
 backend (has secrets, DB volume)                validator (broker, no app secrets)
 ┌────────────────────────────┐   HTTP on an     ┌──────────────────────────────────┐
 │ validation_runner          │   internal-only  │ POST /runs                       │
 │  ValidationExecutor:       │   network        │  - fixed image (pinned digest)   │
 │   LocalExecutor (dev)      │ ───────────────▶ │  - fixed profile enum            │
 │   SandboxExecutor (VM)  ───┼─ tar of tree ──▶ │  - fixed docker run flags        │
 │                            │ ◀─ results ───── │  - per-run volume, then deleted  │
 └────────────────────────────┘                  └───────────────┬──────────────────┘
                                                                 │ (rootless) docker API
                                     ┌───────────────────────────▼─────────────────┐
                                     │ per-run container  (--rm, uid 10001)          │
                                     │  /work = this job's tree copy, nothing else   │
                                     │  install step:  network = sandbox_install     │──▶ registry proxy ──▶ registry.npmjs.org
                                     │  lint/test:     --network none                │
                                     └───────────────────────────────────────────────┘
```

- **Executor seam:** `_run_command_sync` delegates to a `ValidationExecutor`
  protocol. `LocalExecutor` is the phase 1 code, the default for dev.
  `SandboxExecutor` posts to the broker. Selected by
  `VALIDATION_SANDBOX=local|docker`. Profile detection, install gating,
  `ValidationResult` building, truncation and redaction stay in the backend.
- **What crosses the boundary:** in, a tar stream of the working tree
  (`.git` and `node_modules` excluded, same as §3.3) plus a list of profile
  steps from a fixed enum (`npm-ci`, `npm-install`, `npm-lint`, `npm-test`,
  `ruff`, `pytest`). Out, one record per step: exit code, timed-out flag,
  OOM-killed flag, first N KB of output. Never a path on the backend's
  volume, never an image name, never a docker flag. The broker maps the enum
  to argv itself.

### 4.2 Why a broker sidecar, and not the backend calling Docker

| Option | Verdict |
|---|---|
| Mount `/var/run/docker.sock` into the backend | **Rejected.** The Docker socket is root on the host. The backend is the most exposed component (internet-facing behind nginx, parses PDFs, talks to four OAuth providers), and one bug there would become host root |
| Generic docker-socket-proxy (filters by API section) | **Rejected.** "Allow container create" still lets the caller pass `--privileged` or a host bind-mount |
| **Small broker sidecar** with a fixed-shape API | **Recommended.** Its whole job is "run this tree through these enum steps in this image with these flags". A compromised backend can at worst start more validations, rate-limited by the broker. The broker holds no app secrets, and only it touches the container runtime |
| Kubernetes Jobs, Firecracker microVMs | Out of scope for a single compose VM. The broker API is the seam if we ever move |

The broker is about 300–400 lines of Python (FastAPI + the Docker SDK or
`docker` CLI), living in this repo as `sandbox/` with its own Dockerfile and
tests. It authenticates the backend with a shared bearer token
(`VALIDATION_BROKER_TOKEN`, a new backend secret, read via `core/secrets.py`
and registered with the redactor) **and** network placement (only the
backend shares its control network).

**The broker's own blast radius:** whoever controls the broker controls the
container runtime it talks to. Two mitigations, both recommended for
production:

- **Rootless Docker (or Podman) for the sandbox runtime:** a second
  dockerd running as an unprivileged `j2p-sandbox` user on the VM, separate
  from the daemon running the app stack. The broker gets *that* socket, so
  the worst a broker compromise yields is that unprivileged user, not root,
  and never the app stack's daemon or the `backend_var` volume.
- **gVisor (`runsc`) as the runtime for per-run containers.** Tests are
  untrusted code, and container escapes via kernel bugs exist. gVisor puts a
  user-space kernel in between. Cost: installing `runsc` on the VM, and an
  estimated 10–30 % slowdown on syscall-heavy work like `npm ci`. Recommended
  for production, optional for UAT.

### 4.3 Per-run container settings (fixed in the broker)

```
docker run --rm
  --user 10001:10001
  --read-only                                   # image filesystem
  --mount type=volume,src=run-<id>,dst=/work    # this job's tree, nothing else
  --tmpfs /tmp:rw,size=512m,mode=1777
  --tmpfs /home/sandbox:rw,size=256m            # HOME; empty, no .npmrc
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 512
  --memory 2g --memory-swap 2g                  # no swap
  --cpus 2
  --ulimit nofile=4096:4096
  --network <sandbox_install | none>
  --env-file <fixed allowlist, §3.1 values>     # no secrets exist in the broker to leak
  --label j2p.run=<id>                          # for cleanup sweeps
  [--runtime runsc]
  jira2pullreq-validation@sha256:<digest>  <argv from the profile enum>
```

- **Time:** the existing 120 s per command / 300 s install, enforced by the
  broker (`docker wait` with a deadline, then `docker kill`), plus a 15-minute
  ceiling per run. Timeouts become `FAILED` with "timed out", as today.
- **Disk:** the per-run volume gets a size cap where the storage driver
  supports it (overlay2 on XFS with `pquota`); otherwise the broker polls
  usage and kills the run at the limit. `node_modules` is the realistic
  maximum; the proposal is 4 GB.
- **Concurrency:** the broker runs at most N at a time (default 2), queues
  the rest, and rejects beyond a queue depth. That bounds VM load from any
  number of users.
- **Cleanup:** `--rm` plus volume deletion in a `finally`; a startup sweep
  removes leftover `j2p.run` containers and volumes after a broker crash.
- **OOM:** the container exit status reports it. It becomes `FAILED`
  "Validation ran out of memory (limit 2 GB)", so the user isn't told their
  code is wrong for a reason it isn't.

### 4.4 Network

Two Docker networks dedicated to the sandbox, neither connected to `app_net`:

- **`sandbox_install`** (`internal: true`, so no route out). Members: the
  install-step containers and a **registry proxy** (Verdaccio, about 50 MB,
  config: uplink `registry.npmjs.org`, no publishing, no auth). The proxy is
  also on a second network with internet egress. The install step can reach
  the proxy and nothing else: not the backend, not nginx, not the metadata
  endpoint, not the internet. `npm_config_registry` points at the proxy.
- **`none`** for lint and test steps. No network at all.

The proxy doubles as a cache (fast repeat installs without a shared,
writable npm cache) and as the single place to plug in a client's own
Artifactory/Nexus later. A per-repo `.npmrc` that points at a private
registry won't resolve; this is the intended "npm registry only" behaviour
(§5).

Install and test are therefore separate container runs over the same per-run
volume. That is also why install scripts are tolerable in phase 2: a
`postinstall` runs in a container that can only reach the npm proxy, with
nothing secret to read.

### 4.5 Validation image

`deploy/validation/Dockerfile`: Debian slim, Node LTS (pinned), Python 3.12,
`ruff` and `pytest` pinned to the same versions as the backend's dev group,
the corporate CA (same `dsslp-ca.crt` step as the backend image), and a
non-root `sandbox` user (uid 10001). Built by compose, referenced by digest
in the broker's config so a re-tagged image can't slip in. This is also what
fixes §2.5: validation works on the VM for the first time.

### 4.6 Fit with `deploy/vm/docker-compose.yml`

Sketch of the additions (illustrative, not final):

```yaml
services:
  backend:
    environment:
      VALIDATION_SANDBOX: docker
      VALIDATION_BROKER_URL: http://validator:8100
      # VALIDATION_BROKER_TOKEN comes from backend/.env like other secrets
    networks:
      app_net: { ipv4_address: ... }
      sandbox_ctl: {}

  validator:
    build: { context: ../../sandbox }
    restart: unless-stopped
    env_file: [ ${VALIDATOR_ENV_FILE:-../../sandbox/.env} ]   # broker token only
    volumes:
      # The ROOTLESS sandbox daemon's socket, not /var/run/docker.sock.
      - ${SANDBOX_DOCKER_SOCKET:?}:/var/run/docker.sock
    networks: [ sandbox_ctl ]            # NOT app_net, no backend_var volume

  npm-proxy:
    image: verdaccio/verdaccio:<pinned>
    volumes: [ npm_proxy_storage:/verdaccio/storage ]
    networks: [ sandbox_install, egress ]

networks:
  sandbox_ctl:     { internal: true }    # backend <-> validator only
  sandbox_install: { internal: true }    # per-run install containers <-> npm-proxy
  egress: {}                             # npm-proxy -> registry.npmjs.org
```

Two things compose can't express: **per-run containers** (the broker
creates them, which is why it exists), and **the rootless sandbox daemon**
(a host-level systemd unit, documented in `deploy/vm/README.md` with a setup
script). The networks per-run containers join live on the rootless daemon,
so `sandbox_install` and the proxy must be created there too, not by the app
stack's compose. The README's setup section covers that. If the owner
prefers not to run a second daemon in UAT, a single rootful daemon works
with the same broker. It is weaker (broker compromise is host root) but
still keeps untrusted code away from the secrets and the DB. Decision Q2.

### 4.7 Local Windows development

- **Default: `VALIDATION_SANDBOX=local`**, phase 1's hardened subprocess,
  with no Docker required. Isolation is honestly not claimed: it's the
  developer's machine and secrets, and they picked the repo. The job page
  shows "Validation ran unsandboxed (local mode)" in the validation
  section, so nobody mistakes a dev result for the VM's guarantees.
- **Optional:** with Docker Desktop (WSL2), `docker compose --profile
  sandbox up validator npm-proxy` from the repo-root compose, and the
  host-run backend points `VALIDATION_BROKER_URL` at `http://localhost:8100`.
  The workspace crosses as a tar stream, not a bind mount, so Windows paths
  never reach Docker (the root compose file's existing "local repo paths
  don't work in-container" caveat doesn't apply). Rootless and gVisor are
  skipped here, since Docker Desktop's VM is already the isolation layer.
- **Safety net:** the backend refuses `VALIDATION_SANDBOX=local` when
  `AUTH_TRUSTED_PROXY_ENABLED=true` (the marker of a shared deployment)
  unless `VALIDATION_ALLOW_UNSANDBOXED=true` is set explicitly. In that case
  validation returns `SKIPPED` rather than running. A misconfigured VM fails
  safe instead of silently reverting to in-process execution.

### 4.8 Later, not part of this proposal

Installing the repo's Python dependencies (`uv sync`/`pip install -e .`
through a PyPI proxy on `sandbox_install`) is a natural follow-up once the
sandbox exists. Today's pytest runs mostly fail on missing imports anyway.

---

## 5. Trade-offs

| | Phase 0 | Phase 1 | Phase 2 (rootful) | Phase 2 (rootless + gVisor) |
|---|---|---|---|---|
| Secrets via env | ✅ | ✅ | ✅ | ✅ |
| Secrets via `/proc/1/environ` (VM) | ✅ | ⚠️ partial (non-dumpable) | ✅ | ✅ |
| DB / other workspaces / uploads | ✅ | ❌ | ✅ | ✅ |
| Network exfiltration | ✅ | ❌ | ✅ | ✅ |
| Validation tampering with `.git` / commit | ✅ | ✅ | ✅ | ✅ |
| Resource exhaustion | ✅ | ❌ (timeouts only) | ✅ | ✅ |
| Kernel-level container escape | ✅ | n/a | ❌ | ✅ (mitigated) |
| Broker compromise ⇒ host root | n/a | n/a | ❌ yes | ✅ no |
| Validation useful on VM | ❌ (wasn't anyway) | ❌ | ✅ first time | ✅ |
| New ops surface | none | none | broker, proxy, image | + second daemon, `runsc` |
| Windows dev impact | none | none | none (local mode) | none |

Costs and behaviour changes to accept:

- **Speed:** each run starts containers and installs through a cold proxy the
  first time; a cold `npm ci` may take minutes. Repeat installs hit the
  proxy cache. Revalidation after a correction reinstalls unless the per-run
  volume is kept per job, which is possible, at the cost of disk until job
  cleanup (ties into the workspace-retention work in a parallel session).
- **Compatibility:** repos needing a private registry, a non-npm package
  manager (pnpm, yarn berry), native toolchains beyond the image, or git
  metadata during tests will see failures they don't see on a developer
  machine. These show as an honest `FAILED`/`SKIPPED` with the output, never
  as a silent pass. The image and proxy can grow over time.
- **Operability:** two to three new components to run and monitor (broker,
  proxy, optionally the rootless daemon). The broker's `/health` joins the
  compose healthchecks.
- **Residual risk accepted:** code in the sandbox can still read and send
  *its own job's* tree through the npm proxy during install, for example by
  encoding it into package-name lookups. That data is the same client's code
  and is already at the provider. If even that is unacceptable for a client,
  `--ignore-scripts` (Q1) or an offline install from a vetted lockfile is
  the next step.

---

## 6. Test plan

All new tests fail on today's code first (or, for phase 2, on a deliberately
weakened broker config), then pass. That is how this repo has verified
security fixes so far.

### 6.1 New mechanical invariant

Proposed text for CLAUDE.md's "Security invariants" list, numbered after the
existing ones:

> **6. Untrusted repo code never runs with server secrets.** Validation
> commands (the only place this app executes target-repo code) run with an
> allowlisted environment, on a `.git`-less copy of the workspace, and on a
> shared deployment only inside the sandbox. Their output is redacted before
> it is stored. Mechanical checks: `test_validation_isolation.py` (canary
> run) and `test_subprocess_env_policy.py` (static).

Two mechanical checks back it:

**(a) Canary run, `backend/tests/test_validation_isolation.py`.** No mocks
around `subprocess`: it runs the real `validate_workspace` against a
workspace built to attack it.

- Before: set fake values for every secret name `core/secrets.py` knows
  (`ANTHROPIC_API_KEY=sk-ant-canary…`, each `*_CLIENT_SECRET`, each
  `*_ENCRYPTION_KEY`, `JIRA_API_TOKEN`) plus an unknown `ACME_DEPLOY_TOKEN`
  in `os.environ`, and register the known ones via the startup hook.
- The workspace has `pyproject.toml` with `[tool.pytest.ini_options]` and a
  `tests/test_canary.py` that:
  - prints the full `os.environ`, every value, then fails, so it lands in
    `output_excerpt`;
  - writes `sorted(os.environ)` (keys only) to a file in the run directory;
  - runs `git config core.hooksPath …` and writes `.git/hooks/pre-commit`
    from its cwd, as husky's `prepare` or a hostile `postinstall` would;
  - (phase 2, sandbox marker) tries to open the DB and another workspace
    by absolute path, and records which succeeded.
- Assertions:
  - no canary value appears in any `ValidationResult` field;
  - the recorded env keys are exactly the allowlist;
  - the original workspace's `.git/hooks/pre-commit` does not exist, its
    `.git/config` and `git status` are unchanged, and neither is the config
    of a repository *enclosing* the workdir (see §10);
  - (phase 2) the DB/other-workspace opens all failed. In phase 1 they
    succeed: the child has the backend's uid, so a *deliberate* write to
    `../repo/.git` or read of the DB isn't blocked, and isn't tested.
- Plus `ruff.py` in the workspace root that writes a marker file: assert the
  marker isn't created (§3.4).
- A Node variant, skipped when `npm` isn't on PATH, with a `postinstall`
  that prints `process.env`. It runs in CI, where Node exists for the
  frontend.

**(b) Static policy, `backend/tests/test_subprocess_env_policy.py`.** An
AST walk over `backend/app/` that finds every `subprocess.run`, `Popen`,
`check_output`, `asyncio.create_subprocess_exec`/`_shell` call and requires
an explicit `env=` keyword, unless the call site is on a short, commented
allowlist. Today that allowlist would be the trusted git invocations in
`repo_clone.py`/`branch_prep.py`, each with a reason. A new subprocess call
that silently inherits the backend environment then fails CI, the same way
`test_workspace_guard.py` guards agent paths. It doesn't by itself prove
the env is *safe*, but it forces every new call site to make a visible
decision.

### 6.2 Phase 1 unit tests (`test_validation_runner.py`)

- `_validation_env` contains only allowlisted keys; proxy URLs with userinfo
  are dropped; `HOME`/`TMP*`/npm paths point inside the per-run home.
- `_assert_no_secret_values` raises on a registered value and on a
  secret-shaped key; validation then fails closed.
- Redaction happens before truncation: a secret at offset 1,190 doesn't
  leak its first 10 characters.
- The timeout path is redacted too.
- `register_configured_secrets()` registers every reader in `core/secrets.py`
  (the test enumerates the module's `get_*` functions, so a new secret added
  later is covered automatically).
- The validation copy excludes `.git` and `node_modules`; a second run reuses
  the copy's `node_modules`; nothing is copied back.
- `VALIDATION_ENABLED=false` → a single `SKIPPED`, `subprocess.run` never
  called.
- `VALIDATION_SANDBOX=local` with `AUTH_TRUSTED_PROXY_ENABLED=true` and no
  override → `SKIPPED`, nothing runs.

### 6.3 Phase 2 integration tests (Linux + Docker, `@pytest.mark.sandbox`)

They run in a CI job with Docker, against the real broker, image and proxy.
Locally they're skipped unless `VALIDATION_SANDBOX_TESTS=1`.

| Test | Expect |
|---|---|
| Canary from 6.1(a) through `SandboxExecutor` | No canary in output; `/proc/1/environ` has none of them; DB path and `/app/var` don't exist |
| Lint/test step does `curl https://example.com`, `nc backend 8000`, `curl 169.254.169.254` | All fail (no network) |
| Install step reaches the npm proxy; reaches nothing else (`example.com`, backend, metadata) | Proxy OK, rest fail |
| Fork bomb | Step `FAILED`; broker and backend healthy; no leftover processes |
| Allocate 4 GB | `FAILED` "ran out of memory"; container gone |
| `sleep 1000` | `FAILED` "timed out" at the configured limit |
| Fill disk past the cap | `FAILED`; volume removed |
| After every test | `docker ps -a --filter label=j2p.run` and the volume list are empty |
| Broker API: unknown profile, extra JSON fields, image or flag in the body, missing or wrong token | 4xx, nothing started |
| Kill the broker mid-run, restart | Startup sweep removes the orphaned container and volume |
| Concurrency cap | The N+1th run queues; past the queue depth it gets a typed "busy" result |

### 6.4 Manual verification before sign-off on the VM

- Run a real job against a deliberately hostile test repo (canary test plus
  `postinstall`) on the UAT stack: job page shows `[REDACTED]`/no secrets,
  network captures show only proxy traffic during install.
- Run a real job against a normal client repo: validation results match a
  developer machine's for lint and test.
- `docker inspect` a per-run container mid-run: flags as in §4.3, runtime
  `runsc` if enabled.

---

## 7. Related findings (outside this proposal, flagged for follow-up)

- **F1, high: the implementation agent can write `.git/hooks/*` and
  `.git/config`**, which later run inside the backend during create-branch
  and push (§1.5), with the clicking user's write token readable from
  `GIT_CONFIG_PARAMETERS` in `pre-push`. Suggested fix, its own small
  change: `workspace_guard.py` denies any path with a `.git` component, and
  every workspace git call in `branch_prep.py` runs with
  `-c core.hooksPath=<empty dir>`, `-c core.fsmonitor=false`, `--no-verify`
  on commit, and an explicit allowlisted `env=`. Phase 1's "validate on a
  copy" closes only the validation half.
- **F2, medium: `scrubbed_env()` for the agents is a denylist** whose value
  filter depends on lazy registration (§3.1). The agent subprocess (the
  Claude Code CLI) is trusted code, so the risk is lower, but the eager
  registration from §3.2 benefits it immediately, and an allowlist is worth
  considering there too.
- **F3, medium: session IDs are stored in plaintext** in `sessions.id`.
  Storing a hash (cookie value → SHA-256 → lookup) means a DB read alone no
  longer gives session takeover. Independent of validation, but it lowers
  the value of any future DB disclosure.
- **F4, low: VM validation results today are misleading** (§2.5). Phase 0
  addresses it by turning validation off.

## 8. Decisions requested from the owner

(Answered 2026-10-15; see "Owner decisions" at the top.)

1. **Approve phase 0 now** (validation off on the VM via compose)?
2. **Approve phase 1** as specified, including `PR_SET_DUMPABLE` on Linux?
3. **Phase 2 runtime:** rootless sandbox daemon + gVisor (recommended for
   production) or one rootful daemon for UAT first (Q2)?
4. **Q1:** `npm --ignore-scripts` default in phase 1, `false` (proposed) or
   `true`?
5. **Invariant 6** wording and its two mechanical checks (§6.1)?
6. Should F1 go first? It is a separate, small change, and arguably as
   urgent as phase 1, because it bypasses phase 1 through the agent path.

## 9. Effort summary

| Item | Estimate |
|---|---|
| Phase 0: setting, compose value, test | 0.5 day |
| Phase 1: env allowlist, per-run home, redaction, eager registration, `.git`-less copy, `-I`, `PR_SET_DUMPABLE`, tests 6.1–6.2, docs | 2–3 days |
| Phase 2: executor seam, broker (`sandbox/`), validation image, npm proxy, compose and networks, README, tests 6.3 | 7–10 days |
| Phase 2 hardening: rootless sandbox daemon setup script + docs, gVisor install + perf check | 1–2 days |
| F1 (git hooks / `.git` writes) | 1 day |

## 10. Phases 0 and 1 as built (2026-10-15)

Code: `backend/app/steps/validation_runner.py`, `core/secrets.py`
(`register_configured_secrets`), `core/process_hardening.py`, `core/config.py`
(`VALIDATION_ENABLED`, `VALIDATION_ALLOW_UNSANDBOXED`), `main.py` (startup),
`deploy/vm/docker-compose.yml`. Tests: `test_validation_runner.py`,
`test_validation_isolation.py`, `test_subprocess_env_policy.py`. Runbook text:
CLAUDE.md "Validation isolation", `deploy/vm/README.md`.

Where it differs from the sections above:

- **`GIT_CEILING_DIRECTORIES` added to the environment (§3.1/§3.3).** Found
  while writing the canary: the validation copy has no `.git`, so git run
  from it searches upward — and in local dev the workdir is inside this app's
  own checkout. Husky's `prepare` (`git config core.hooksPath .husky`) would
  have rewritten the *app's* git config. The ceiling is the job directory;
  the canary nests the job inside a stand-in "app" repository and asserts
  its config is unchanged.
- **The shared-deployment guard (§4.7) landed in phase 1**, without a
  `VALIDATION_SANDBOX` setting: there is only the local executor, so the rule
  is "trusted-proxy auth ⇒ skipped unless `VALIDATION_ALLOW_UNSANDBOXED=true`".
  §6.2's last test is written against that.
- **`npm --ignore-scripts`: not added** (decision Q1).
- **Static-check allowlist (§6.1 b)** turned out to be two call sites, not
  the ones guessed: `jobs/runner.py:_run_git_command` (diff collection) and
  `steps/branch_prep.py:_validate_branch_name` (`git check-ref-format`). Every
  other launch already passed `env=` (possibly `None` conditionally, which
  the check accepts as a visible decision). A stale-entry test keeps the
  allowlist honest.
- **Redaction runs before truncation on all three result paths**
  (pass, fail, timeout), and `register_configured_secrets()` runs both at
  startup and at the start of each validation, so tests and any non-server
  entry point get the same guarantee.
- **The per-run home is removed after each validation run; the validation
  copy is kept** for its `node_modules` and removed with the job directory by
  workspace retention.
