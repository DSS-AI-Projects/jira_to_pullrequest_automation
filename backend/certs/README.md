# Extra CA certificates (optional)

Drop any additional trusted root/intermediate CA certificates here, as
**`.crt` files, PEM-encoded** (Debian's `update-ca-certificates` only scans
`*.crt` under this directory — a `.pem` file here is silently ignored, not
an error, just not trusted; rename it to `.crt` if that's what you have),
before running `docker compose build`. They are baked into the backend
image's trust store at build time (`backend/Dockerfile`) via
`update-ca-certificates`, so `truststore` (`app/main.py`) — and therefore
every outbound `httpx` call the backend makes (Jira/GitHub/GitLab OAuth, the
Anthropic SDK) — trusts them too.

This directory is empty by default; `update-ca-certificates` is a no-op
when there is nothing to add, so a build with no certs here behaves exactly
as before.

## When you need this

A self-hosted provider instance (e.g. an internal GitLab behind your
company's own CA, the way `GITLAB_INSTANCE_URL` supports) presents a
certificate the public CA bundle doesn't recognize — even though your
browser already trusts it (via the OS/domain-joined trust store), a fresh
container's OS trust store starts out with only the public CAs `apt-get
install ca-certificates` provides. Without the matching CA cert here, the
backend's own calls to that instance fail with
`CERTIFICATE_VERIFY_FAILED`, even though the browser leg of an OAuth
redirect to the same instance works fine.

## How to get the certificate

Export your internal CA's root certificate in PEM format, or fetch the
server's certificate chain directly, e.g.:

```bash
openssl s_client -connect your-gitlab-instance.example.com:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > backend/certs/internal-ca.crt
```

Then rebuild the backend image so the new cert is baked in:

```bash
docker compose build --no-cache backend
docker compose up -d backend
```

`.crt`/`.pem` files placed here are gitignored — never committed, since
they're deployment-specific, not application code.
