## VM deployment with Entra ID sign-in

A ready-to-run stack for a Linux VM (UAT or production): users sign in with
their Microsoft Entra ID account, and the app's development login is off.

| File | What it is |
| --- | --- |
| `docker-compose.yml` | nginx + oauth2-proxy + backend + frontend on a private network; only nginx is published |
| `nginx.conf.template` | TLS, Entra sign-in on every page and API call, identity headers for the backend, upload and timeout limits |
| `vm.env.example` | Non-secret settings (hostname, certificate files, network range, data volume); copy to `vm.env.local` |

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
2. **Redirect URI**: type **Web**, `https://<APP_HOSTNAME>/oauth2/callback`.
3. Copy the **Application (client) ID** and **Directory (tenant) ID**.
4. **Certificates & secrets** → **New client secret**; copy the value (shown
   once).
5. **API permissions**: Microsoft Graph delegated `openid`, `profile`, `email`;
   grant admin consent if your organization requires it.
6. Recommended: **Enterprise applications** → the app → **Properties** →
   **Assignment required = Yes**, then add the allowed users or groups.

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
- `APP_SUBNET_PREFIX`: keep `172.30.0` unless `ip route` or
  `docker network ls` shows it in use.
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

1. In a private browser window, open `https://<APP_HOSTNAME>`: Microsoft
   sign-in, then the app, with no development-login form.
2. Spoofed identity headers are ignored; the output must not contain a user:

   ```bash
   curl -sk https://localhost/api/auth/session -H 'X-Auth-Request-Email: admin@example.com' | head -c 300
   ```

3. The backend isn't reachable directly; this must fail to connect:

   ```bash
   curl -s --max-time 5 http://localhost:8000/health
   ```

4. On a job page, the Push card still shows your connected accounts (if your
   Entra email matches your previous login email).

### Notes

- **Logging out**: the app's Log out clears only the app session; the Entra
  session signs you straight back in. Full sign-out:
  `https://<APP_HOSTNAME>/oauth2/sign_out`.
- **Rollback**: `down` this stack and start the previous one; the data volume
  is untouched either way.
- **Multiple team instances on one VM**: give each its own
  `COMPOSE_PROJECT_NAME`, `BACKEND_VAR_VOLUME`, `APP_SUBNET_PREFIX`,
  `PUBLIC_HTTPS_PORT` (or host name behind a shared proxy), and its own
  `backend/.env`.
- The generic reference topology and other identity providers:
  `deploy/README.md` and `deploy/AZURE_ENTRA_ID.md`.
