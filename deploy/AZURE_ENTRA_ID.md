## Azure AD / Entra ID Setup

This guide plugs Microsoft Entra ID into the shared-auth deployment bundle in
this repository.

### When to use this

Use this profile when your team already uses Microsoft 365 / Entra ID and you
want browser authentication handled by `oauth2-proxy` in front of the app.

### Files involved

- `deploy/.env.shared-auth.example`
- `deploy/docker-compose.shared-auth.example.yml`
- `deploy/oauth2-proxy/azure-entra.env.example`
- `deploy/nginx/oauth2-proxy.compose.conf.example`

### 1. Create the Entra app registration

In Microsoft Entra admin center:

1. Create a new app registration.
2. Choose a single-tenant app unless you explicitly need multi-tenant auth.
3. Add a **Web** redirect URI:
   `https://<your-app-hostname>/oauth2/callback`
4. Create a client secret and save it in your deployment secret store.
5. Add delegated Microsoft Graph permissions:
   - `openid`
   - `profile`
   - `email`
6. Grant admin consent if your org requires it.

### 2. Prepare the oauth2-proxy secret env file

Copy `deploy/oauth2-proxy/azure-entra.env.example` to a secret file outside git
and fill in:

- `OAUTH2_PROXY_OIDC_ISSUER_URL`
- `OAUTH2_PROXY_CLIENT_ID`
- `OAUTH2_PROXY_CLIENT_SECRET`
- `OAUTH2_PROXY_COOKIE_SECRET`

Example issuer:

```env
OAUTH2_PROXY_OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant-id>/v2.0
```

### 3. Prepare the shared deployment env file

Copy `deploy/.env.shared-auth.example` to something like
`deploy/.env.shared-auth.local` and set:

```env
APP_HOSTNAME=app.example.com
BACKEND_ENV_FILE=../backend/.env
OAUTH2_PROXY_ENV_FILE=./oauth2-proxy/azure-entra.env.local
TLS_DIR=./tls
TRUSTED_PROXY_SOURCE=172.30.0.10/32
```

### 4. Prepare backend auth settings

Your `backend/.env` should include the shared-deployment auth settings:

```env
AUTH_ENABLED=true
AUTH_ALLOW_DEV_LOGIN=false
AUTH_SESSION_COOKIE_SECURE=true
AUTH_TRUSTED_PROXY_ENABLED=true
```

The compose file injects:

- `AUTH_TRUSTED_PROXY_SOURCES`
- `CORS_ORIGINS`

from `deploy/.env.shared-auth.local`.

### 5. Claim choice: email vs preferred username

This example defaults to:

```env
OAUTH2_PROXY_OIDC_EMAIL_CLAIM=preferred_username
```

Reason:

- many Entra tenants do not emit an `email` claim for managed users by default
- `preferred_username` is commonly present and is usually the user's UPN

If your tenant includes a real `email` claim in the ID token and you prefer to
use it, change the value to:

```env
OAUTH2_PROXY_OIDC_EMAIL_CLAIM=email
```

### 6. Optional group restriction

If you want to restrict access to one or more Entra groups, set:

```env
OAUTH2_PROXY_ALLOWED_GROUPS=<group-object-id-1>,<group-object-id-2>
```

Use Entra group object IDs, not display names.

### 7. Run the stack

Use the parameterized compose flow:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  config --services
```

Then build and start:

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  build backend frontend
```

```powershell
docker compose --env-file deploy/.env.shared-auth.local `
  -f deploy/docker-compose.shared-auth.example.yml `
  up -d
```

### 8. Validate the login flow

1. Open `https://<your-app-hostname>`.
2. Confirm you are redirected to Microsoft sign-in.
3. Complete sign-in.
4. Confirm the app loads and `/api/auth/session` returns a populated `user`.
5. Confirm direct spoofed `X-Auth-Request-*` headers do not create a session.

### Common pitfalls

- Wrong redirect URI:
  Entra must have `https://<your-app-hostname>/oauth2/callback` exactly.
- Wrong tenant issuer:
  use the real tenant ID unless you intentionally want multi-tenant auth.
- Missing claim:
  if the user reaches Entra but app identity resolution is blank, check whether
  your ID token includes `email` or `preferred_username`.
- Secret leakage:
  keep the filled env file outside git; do not run `docker compose ... config`
  without understanding that it can expand and print env file values.
