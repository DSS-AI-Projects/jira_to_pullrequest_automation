## VM deployment with Entra ID sign-in

A ready-to-run stack for a Linux VM (UAT or production): users sign in with
their Microsoft Entra ID account, and the app's development login is off.

| File | What it is |
| --- | --- |
| `docker-compose.yml` | nginx + oauth2-proxy + backend + frontend on a private network; only nginx is published |
| `nginx.conf.template` | TLS, Entra sign-in on every page and API call, identity headers for the backend, upload and timeout limits |
| `vm.env.example` | Non-secret settings (hostname, certificate files, network range, data volume, backups, log rotation); copy to `vm.env.local` |
| `backup/sqlite_backup.py` | Database snapshots, verification and restore, run by the `backup` and `db-restore` services |

Secrets stay out of these files: the Entra client and cookie secrets go in
`deploy/oauth2-proxy/azure-entra.env.local`, everything else in
`backend/.env`. Both `*.env.local` files are git-ignored.

### How sign-in works

1. nginx sends every request through oauth2-proxy first; without a session the
   user is redirected to Microsoft sign-in.
2. After sign-in, nginx forwards requests with `X-Auth-Request-Email`, `-Name`
   and `-User` headers taken from oauth2-proxy's answer, overwriting anything
   the client sent.
3. The backend trusts those headers only from nginx's fixed internal address
   (`<APP_SUBNET_PREFIX>.10`). The backend publishes no port, so nothing else
   can reach it.
4. Users are matched to existing app accounts **by email**. Someone whose
   Entra email (usually their UPN) matches the email they used with
   development login keeps their jobs and connected GitHub, GitLab and
   Bitbucket accounts.

### 1. Register the app in Entra ID

Needs Entra admin rights. In the Microsoft Entra admin center:

1. **App registrations** → **New registration**: single tenant
   ("Accounts in this organizational directory only").
2. **Redirect URIs**, both of type **Web**:
   `https://<APP_HOSTNAME>/oauth2/callback` (sign-in) and
   `https://<APP_HOSTNAME>/signin/signed-out` (where Sign out returns;
   Microsoft refuses an unregistered post-logout address).
3. Copy the **Application (client) ID** and **Directory (tenant) ID**.
4. **Certificates & secrets** → **New client secret**; copy the value (shown
   once).
5. **API permissions**: Microsoft Graph delegated `openid`, `profile`, `email`;
   grant admin consent if your organization requires it.
6. Recommended: **Enterprise applications** → the app → **Properties** →
   **Assignment required = Yes**, then add the allowed users or groups.
7. Optional: **Company branding** (Entra ID → Custom branding) puts your logo,
   background and sign-in text on Microsoft's password page. It applies to
   the whole tenant, not just this app.

### 2. Create the secret and settings files on the VM

```bash
cp deploy/oauth2-proxy/azure-entra.env.example deploy/oauth2-proxy/azure-entra.env.local
chmod 600 deploy/oauth2-proxy/azure-entra.env.local
python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

In `azure-entra.env.local` set `OAUTH2_PROXY_OIDC_ISSUER_URL`
(`https://login.microsoftonline.com/<tenant-id>/v2.0`),
`OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`, and
`OAUTH2_PROXY_COOKIE_SECRET` (the printed value).

```bash
cp deploy/vm/vm.env.example deploy/vm/vm.env.local
```

In `vm.env.local` set:

- `APP_HOSTNAME` and the certificate files (`TLS_DIR`, `TLS_CERT_FILE`,
  `TLS_KEY_FILE`).
- `ENTRA_TENANT_ID`: the Directory (tenant) ID, for the Microsoft sign-out.
- `APP_SUBNET_PREFIX`: keep `172.30.0` unless `ip route` or
  `docker network ls` shows it in use.
- `BACKUP_HOST_DIR`: where daily database snapshots go on the VM (default
  `/var/backups/jira2pullreq`). Create it before starting the stack:
  `sudo install -d -o 1000 -g 1000 -m 700 /var/backups/jira2pullreq`
  (uid 1000 is the backend image's `appuser`; a folder Docker creates itself
  is owned by root and the backup service can't write to it). See
  [5. Backups](#5-backups).
- `BACKEND_VAR_VOLUME`: **when moving from the repo-root stack**, set it to that
  stack's existing data volume (`docker volume ls`, e.g.
  `<folder>_backend_var`) so jobs, users and provider connections carry over.
  Docker may warn the volume "was not created by Docker Compose"; that's
  expected and harmless.

In `backend/.env`, set `AUTH_ADMIN_EMAILS` to admins' **Entra** emails and
`PUSH_AUTH_MODE=delegated`. The compose file forces the sign-in settings
(`AUTH_*`, `CORS_ORIGINS`), so values for those in `backend/.env` are ignored.

### 3. Start it

Stop the repo-root stack first if it's running (`docker compose down` from the
repo root, without `-v`, keeps its data volume). Back up the database first:

```bash
docker compose cp backend:/app/var/jobs.db ./jobs.db.backup-$(date +%F)
docker compose down
```

Then, from the repo root:

```bash
docker compose --env-file deploy/vm/vm.env.local -f deploy/vm/docker-compose.yml up -d --build
docker compose --env-file deploy/vm/vm.env.local -f deploy/vm/docker-compose.yml exec nginx nginx -t
```

Remove sessions created through development login, which would otherwise stay
valid for up to 12 hours:

```bash
docker compose --env-file deploy/vm/vm.env.local -f deploy/vm/docker-compose.yml exec -T backend \
  python3 -c "import sqlite3; c=sqlite3.connect('/app/var/jobs.db'); n=c.execute('DELETE FROM sessions').rowcount; c.commit(); print(n, 'old sessions removed')"
```

### 4. Verify

1. In a private browser window, open `https://<APP_HOSTNAME>/jobs`: the app's
   own sign-in page → **Sign in with Microsoft** → Microsoft's account picker
   and password → back on `/jobs`. No development-login form anywhere.
2. **Sign out** (top right) ends on "You have signed out"; signing in again
   asks for the account again.
3. Spoofed identity headers are ignored; this must answer 401 "Your sign-in
   has expired", not a user:

   ```bash
   curl -sk https://localhost/api/auth/session -H 'X-Auth-Request-Email: admin@example.com' | head -c 300
   ```

4. The backend isn't reachable directly; this must fail to connect:

   ```bash
   curl -s --max-time 5 http://localhost:8000/health
   ```

5. On a job page, the Push card still shows your connected accounts (if your
   Entra email matches your previous login email).

### 5. Backups

Everything the app keeps — users, sessions, jobs and their plans, and the
encrypted provider connections — is in one SQLite file, `/app/var/jobs.db`,
on the `BACKEND_VAR_VOLUME` volume. The `backup` service protects it:

- Every `BACKUP_INTERVAL_HOURS` (default 24) it takes a snapshot with
  SQLite's online backup API (a consistent copy even while the backend is
  writing; a plain file copy can catch a half-written transaction), checks it
  with `PRAGMA integrity_check`, and only then saves it as
  `BACKUP_HOST_DIR/jobs-<UTC time>.db` (mode 600). It keeps the newest
  `BACKUP_KEEP` (default 14) and deletes older ones. A failed snapshot is
  retried after 15 minutes and never deletes a good one.
- The schedule follows the newest snapshot, so restarting the stack doesn't
  add extra snapshots: on start it takes one right away only if there is
  none yet or the newest is older than the interval.
- The service has no network, no Linux capabilities, a read-only root
  filesystem, and the data volume **read-only**: it can't change the database.
- `docker compose ps` shows it **unhealthy** when the newest snapshot is more
  than an hour overdue; `logs backup` says why (for example, a
  `BACKUP_HOST_DIR` it can't write to).

Use the same `--env-file … -f …` as in step 3 (shortened to `dc` here):

```bash
alias dc='docker compose --env-file deploy/vm/vm.env.local -f deploy/vm/docker-compose.yml'
dc ps backup                        # healthy?
dc logs --tail 20 backup            # "snapshot … written, integrity_check ok"
dc run --rm db-restore              # list snapshots, newest first
dc exec backup python /opt/backup/sqlite_backup.py once   # take one now
```

#### Copy snapshots off the VM

Snapshots on the VM's own disk don't survive losing the VM or its disk.
Copy them somewhere else daily, after the snapshot — for example, pulled by a
backup server:

```bash
rsync -a --chmod=F600 vm-host:/var/backups/jira2pullreq/ /srv/backups/jira2pullreq/
```

or pushed from the VM by a root cron job to an Azure Storage container
(with a lifecycle rule for retention), using a managed identity:

```bash
azcopy login --identity
azcopy sync /var/backups/jira2pullreq "https://<account>.blob.core.windows.net/<container>/jira2pullreq" --delete-destination=false
```

Snapshots contain users' emails, ticket and plan content, and encrypted
provider tokens: give the destination the same access control as the VM.
To check a copied file anywhere with Python 3.11+:
`python3 deploy/vm/backup/sqlite_backup.py verify jobs-<time>.db`.

#### Back up the Fernet keys separately

The Jira, GitHub, GitLab and Bitbucket tokens in the database are encrypted
with `JIRA_OAUTH_ENCRYPTION_KEY`, `GITHUB_OAUTH_ENCRYPTION_KEY`,
`GITLAB_OAUTH_ENCRYPTION_KEY` and `BITBUCKET_OAUTH_ENCRYPTION_KEY` from
`backend/.env`. **A snapshot restored without the same keys loses every
provider connection** (users must reconnect each one; jobs and users are
still there). The keys are not in the snapshots, and must never be stored
with them — whoever has both can decrypt the tokens:

- Keep each key as a secret in Azure Key Vault (or your organization's
  secret store), with access separate from the backup storage.
- When a key is rotated, keep the old value until no snapshot that needs it
  is retained anywhere.
- Back up `backend/.env` and `deploy/oauth2-proxy/azure-entra.env.local` the
  same way; the other settings in them can be re-created, the keys can't.

#### Restore

1. Stop the backend and the backup service (users get an error page until
   step 4):

   ```bash
   dc stop backend backup
   ```

2. Pick a snapshot (`dc run --rm db-restore`). One copied back from off the
   VM goes into `BACKUP_HOST_DIR` first, readable by uid 1000.
3. Restore it:

   ```bash
   dc run --rm db-restore restore /backups/jobs-20261021T023000Z.db
   ```

   It re-checks the snapshot's integrity, refuses if the database is still
   in use, saves the current database beside it as
   `jobs.db.pre-restore-<time>` (so the restore can be undone the same way),
   removes stale `-journal`/`-wal`/`-shm` files, and swaps the snapshot in.
4. Start everything again and check a job page:

   ```bash
   dc up -d
   ```

The `pre-restore` copies stay on the data volume until removed by hand
(`dc exec backend sh -c 'rm /app/var/jobs.db.pre-restore-*'`).

On a new VM: follow steps 1–3 with the **same Fernet keys** (from the key
vault, not a snapshot), then restore as above — the stack's first start
creates the volume and an empty database, which the restore replaces.

#### Container logs

Every service logs through Docker's `json-file` driver capped at
`DOCKER_LOG_MAX_FILE` files of `DOCKER_LOG_MAX_SIZE` (default 5 × 10 MB per
container); older log lines are dropped.

### Notes

- **Signing in and out**: signed-out visitors land on `/signin` (public, like
  `/signin/signed-out`, the page's build files and `/health`; everything else
  needs a session). Microsoft shows its account picker on every sign-in
  (`OAUTH2_PROXY_PROMPT=select_account`), so a shared browser never silently
  continues as the previous user. **Sign out** clears the app session, then
  oauth2-proxy's cookie, then the Microsoft session in that browser — which
  also signs the user out of other Microsoft web apps there (Outlook, Teams).
  If a session expires while a page is open, the next action goes to
  `/signin` and back.
- **Validation is off on this stack** (`VALIDATION_ENABLED: "false"` in
  `docker-compose.yml`, forced like the auth settings). Validation runs the
  target repository's own code (npm install scripts, lint and test scripts,
  pytest), and without a sandbox that code could read the server's secrets,
  its database and other users' workspaces. Jobs still plan, implement, show
  the diff, and create and push branches; the validation section reports
  "Validation is disabled on this deployment" and no automatic fix is offered.
  It comes back once validation runs in a throwaway sandbox container
  (`docs/validation-isolation-proposal.md`, phase 2). A value in
  `backend/.env` can't turn it back on (compose's `environment:` wins), and
  even with it on, a backend with trusted-proxy sign-in skips validation
  unless `VALIDATION_ALLOW_UNSANDBOXED=true` is also set.
- **Mantis issues as a requirement source** (optional): the app reads a
  Mantis issue with **one shared, read-only Mantis account**, since Mantis has
  no OAuth and the app never takes a pasted token. In Mantis, create a
  dedicated user with the *viewer* access level and access to only the
  projects whose teams use this app, then create an API token for it (My
  Account → API Tokens). In `backend/.env` set `MANTIS_ENABLED=true`,
  `MANTIS_BASE_URL=https://<your mantis>` (https is required: the token
  travels in a header), `MANTIS_API_TOKEN=<that token>`, and optionally
  `MANTIS_KEY_PREFIX` (jobs are named `MANTIS-1234` by default) and
  `MANTIS_ATTACHMENT_FETCH_ENABLED=true` for PDF attachments. If your Mantis
  rejects every API token (`403 API token not found`, seen on a customized
  2.25.2), use its SOAP API instead: set `MANTIS_API=soap`,
  `MANTIS_USERNAME=<that account>` and `MANTIS_PASSWORD=<its password>` in
  place of `MANTIS_API_TOKEN`. Use a dedicated *local* Mantis account for
  this, never a person's or an SSO/LDAP one. Restart the
  backend; the job form then shows **Mantis issue**. Every signed-in user can
  fetch any issue that account can see, so keep its project list tight. The
  VM must reach the Mantis server; an internal certificate works if the CA is
  in the backend image (see `backend/certs/`).
- **Local repositories** (optional): jobs can use a repository in a folder on
  the host instead of a remote URL. The backend is a Linux container, so it
  sees only folders mounted into it, and the job form takes the path *inside*
  the container. `docker-compose.local-repos.yml` mounts
  `LOCAL_REPOS_HOST_DIR` (set it in `vm.env.local`, e.g. `/srv/repos`, or
  `D:/repos` under Docker Desktop) read-only at `/repos`, plus
  `gitconfig-local-repos` as the container's `/etc/gitconfig`. Without that
  file git refuses the repositories ("detected dubious ownership"): their
  files belong to another uid than the backend's uid 1000 (root, for a
  Windows folder). In `backend/.env` set:

  ```
  ALLOW_LOCAL_REPOS=true
  ALLOWED_LOCAL_REPO_ROOTS=["/repos"]
  ```

  Then start the stack with both compose files, and pass both on every later
  command (an `up` without the override recreates the backend without the
  mount):

  ```bash
  docker compose --env-file deploy/vm/vm.env.local \
    -f deploy/vm/docker-compose.yml -f deploy/vm/docker-compose.local-repos.yml up -d
  ```

  On the job form, a repository at `<LOCAL_REPOS_HOST_DIR>/my-service` is
  `/repos/my-service`; a host path such as `D:\repos\my-service` fails.
  Every signed-in user can use any repository under the folder, so mount only
  what all of them may see. Pushing goes to the repository's own `origin`,
  which for delegated push must be a GitHub, GitLab or Bitbucket remote (an
  SSH origin is pushed over https). `ALLOW_DIRTY_LOCAL_REPOS`,
  `REQUIRE_LOCAL_BRANCH_TICKET_MATCH` and `ALLOW_LOCAL_NON_GIT_FOLDERS` work
  as in local development.
- **Rollback**: `down` this stack and start the previous one; the data volume
  is untouched either way.
- **Multiple team instances on one VM**: give each its own
  `COMPOSE_PROJECT_NAME`, `BACKEND_VAR_VOLUME`, `APP_SUBNET_PREFIX`,
  `PUBLIC_HTTPS_PORT` (or host name behind a shared proxy), and its own
  `backend/.env`.
- The generic reference topology and other identity providers:
  `deploy/README.md` and `deploy/AZURE_ENTRA_ID.md`.
