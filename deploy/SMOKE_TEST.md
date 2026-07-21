## Container Smoke Test

This runbook validates the image-based shared-auth stack defined in
`deploy/docker-compose.shared-auth.example.yml`.

### Preconditions

- Docker Desktop is installed and the Docker engine is running.
- `backend/.env` exists with real runtime values for Jira and Anthropic.
- `deploy/oauth2-proxy/oauth2-proxy.env.example` has been copied to a real
  secret env file outside git and populated with your OIDC values.
- TLS certificate files exist for the nginx mount path used by the compose
  stack.
- `deploy/.env.shared-auth.example` has been copied to a real deployment env
  file and updated with your hostname and file paths.

### Recommended local prep

1. Copy `deploy/.env.shared-auth.example` to a deployment-specific file such as
   `deploy/.env.shared-auth.local`.
2. Set `APP_HOSTNAME` to your real hostname.
3. Set `OAUTH2_PROXY_ENV_FILE` to your real oauth2-proxy secret env file.
4. Set `TLS_DIR` to your actual certificate mount directory.
5. If needed, set `PUBLIC_HTTPS_PORT` and `TRUSTED_PROXY_SOURCE`.

### Syntax validation

Run this first to confirm the compose file resolves correctly:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  config --services
```

Use the full `docker compose ... config` output only with care: it expands
`env_file` values and can print secrets from `backend/.env` into your terminal
or CI logs.

### Build images

Build the backend and frontend images:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  build backend frontend
```

### Start the stack

Start all services in detached mode:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  up -d
```

Watch service logs if anything fails:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  logs -f
```

### Health and routing checks

Confirm containers are running:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  ps
```

Confirm nginx can reach the backend health endpoint through the public origin:

```powershell
curl.exe -k https://your-real-hostname.example/health
```

Expected result:

- HTTP `200`
- body contains `{"status":"ok"}`

### Auth flow checks

1. Open `https://your-real-hostname.example`.
2. Confirm the upstream sign-in flow is triggered by `oauth2-proxy`.
3. Complete sign-in with the configured identity provider.
4. After redirect back, confirm the app loads instead of the sign-in gate.
5. Confirm the signed-in banner appears in the UI.

### Backend session check

After signing in through the browser, verify the session API through the public
origin:

```powershell
curl.exe -k https://your-real-hostname.example/api/auth/session
```

Expected result:

- `auth_enabled` is `true`
- `user` is populated
- `can_dev_login` is `false`

### Header spoofing check

Direct spoofing from outside the trusted proxy path must fail. A request like
this must not create an authenticated session:

```powershell
curl.exe -k `
  -H "X-Auth-Request-Email: attacker@example.com" `
  -H "X-Auth-Request-User: attacker" `
  -H "X-Auth-Request-Name: Attacker" `
  https://your-real-hostname.example/api/auth/session
```

Expected result:

- no authenticated `user` unless the request is already coming through the real
  authenticated proxy flow

### App API check

Confirm same-origin frontend API routing works without
`NEXT_PUBLIC_API_BASE_URL`:

1. Load the app through `https://your-real-hostname.example`.
2. Open browser devtools network tab.
3. Confirm app requests go to `/api/...` on the same origin.
4. Confirm there is no browser attempt to call `:8000` directly.

### Cleanup

Stop and remove the stack:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  down
```
