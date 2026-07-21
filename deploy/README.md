## Trusted Proxy Deployment

This directory contains deployment examples for shared/team environments where
authentication is handled by an upstream auth gateway instead of the built-in
dev login flow.

### Bundle contents

- `deploy/.env.shared-auth.example`: one place to set hostname, TLS path, and env file paths
- `deploy/nginx/oauth2-proxy.conf.example`: reverse proxy on the public origin
- `deploy/nginx/oauth2-proxy.compose.conf.example`: compose-friendly reverse proxy
- `deploy/oauth2-proxy/oauth2-proxy.env.example`: upstream auth gateway env template
- `deploy/oauth2-proxy/azure-entra.env.example`: Microsoft Entra ID flavored oauth2-proxy template
- `deploy/docker-compose.shared-auth.example.yml`: example multi-service topology
- `deploy/AZURE_ENTRA_ID.md`: provider-specific setup guide for Microsoft Entra ID
- `backend/Dockerfile` and `frontend/Dockerfile`: production-oriented image builds
- `backend/.dockerignore` and `frontend/.dockerignore`: lean image build contexts

### Recommended topology

Use a single external origin and keep the backend private behind the reverse
proxy:

- Browser -> `nginx` on `https://app.example.com`
- `nginx` -> `oauth2-proxy` on `127.0.0.1:4180`
- `nginx` -> Next.js frontend on `127.0.0.1:3000`
- `nginx` -> FastAPI backend on `127.0.0.1:8000`

This setup has three benefits:

- The browser talks to one origin, so frontend API calls do not need a separate
  `NEXT_PUBLIC_API_BASE_URL`.
- The backend is not exposed directly to the internet.
- Identity headers are injected only by the trusted proxy path.

### Backend settings

Configure `backend/.env` like this for shared deployment:

```env
AUTH_ENABLED=true
AUTH_ALLOW_DEV_LOGIN=false
AUTH_SESSION_COOKIE_SECURE=true
AUTH_TRUSTED_PROXY_ENABLED=true
AUTH_TRUSTED_PROXY_SOURCES=["127.0.0.1/32","::1/128"]
AUTH_TRUSTED_EMAIL_HEADER=X-Auth-Request-Email
AUTH_TRUSTED_NAME_HEADER=X-Auth-Request-Name
AUTH_TRUSTED_SUBJECT_HEADER=X-Auth-Request-User
CORS_ORIGINS=["https://app.example.com"]
```

Adjust `AUTH_TRUSTED_PROXY_SOURCES` to the actual source IPs or CIDR ranges
from which your reverse proxy reaches the backend.

The compose example in this directory uses a static internal network and sets
`AUTH_TRUSTED_PROXY_SOURCES` from `deploy/.env.shared-auth.example` so the
backend only trusts the `nginx` service IP on that network.

### Frontend settings

If you proxy `/api/` on the same external origin, leave
`NEXT_PUBLIC_API_BASE_URL` unset so the frontend resolves API requests relative
to the current host.

Only set `NEXT_PUBLIC_API_BASE_URL` when you intentionally run the frontend and
backend on different origins. If you do that, update `CORS_ORIGINS` on the
backend to match the browser origin.

### nginx example

See `deploy/nginx/oauth2-proxy.conf.example` for a concrete example that:

- delegates browser auth to `oauth2-proxy`
- forwards the frontend and backend on one public origin
- injects `X-Auth-Request-Email`, `X-Auth-Request-Name`, and
  `X-Auth-Request-User` for the backend

For the image-based compose stack, use
`deploy/nginx/oauth2-proxy.compose.conf.example` instead. It uses docker
service names (`frontend`, `backend`, `oauth2-proxy`) on the internal network.
The compose file mounts it into `/etc/nginx/templates/default.conf.template`,
which lets the official nginx image substitute `${APP_HOSTNAME}` at container
startup.

### oauth2-proxy example

See `deploy/oauth2-proxy/oauth2-proxy.env.example` for the minimum OIDC-facing
settings expected by the reverse proxy example.

You still need to replace all placeholders with real deployment values and keep
the resulting secret material outside git.

For Microsoft 365 / Entra-backed teams, start with
`deploy/oauth2-proxy/azure-entra.env.example` and the companion guide
`deploy/AZURE_ENTRA_ID.md`.

### docker-compose example

See `deploy/docker-compose.shared-auth.example.yml` for a reference topology
that runs:

- `backend` on an internal container IP
- `frontend` on an internal container IP
- `oauth2-proxy` as the upstream auth gateway
- `nginx` as the only public entry point and the only trusted backend caller

This compose file builds the backend and frontend from:

- `backend/Dockerfile`
- `frontend/Dockerfile`

The build contexts are reduced by:

- `backend/.dockerignore`
- `frontend/.dockerignore`

This example is intended as a deployment reference. For production, publish the
built images to your registry and pin them by version instead of rebuilding from
source on every host.

### Recommended customization flow

1. Copy `deploy/.env.shared-auth.example` to a deployment-specific file such as
   `deploy/.env.shared-auth.local`.
2. Set `APP_HOSTNAME`, `TLS_DIR`, `BACKEND_ENV_FILE`, and
   `OAUTH2_PROXY_ENV_FILE`.
3. Point `OAUTH2_PROXY_ENV_FILE` at a real secret env file outside git.
4. Run compose with `--env-file deploy/.env.shared-auth.local`.

### Provider-specific guide

The first provider-specific guide in this repo is:

- `deploy/AZURE_ENTRA_ID.md` for Microsoft Entra ID / Azure AD

### Validation checklist

After deployment:

1. Confirm the backend is reachable only from the reverse proxy network.
2. Confirm direct requests with spoofed `X-Auth-Request-*` headers do not reach
   the backend from outside the proxy allowlist.
3. Confirm `/api/auth/session` returns an authenticated user when reached
   through the proxy after sign-in.
4. Confirm logging out through the app clears the app session and returns the
   browser to the upstream sign-in flow when needed.
5. Confirm the frontend reaches `/api/...` on the public origin without needing
   `NEXT_PUBLIC_API_BASE_URL` in the same-origin proxy setup.
6. Confirm the deployed stack uses the compose nginx config rather than the
   host-loopback nginx config.
