# jira2pullreq

Reads a Jira ticket, analyzes a Git repo, and produces a structured implementation
plan (Milestone 1). Later milestones will apply the change and open a PR.

See [CLAUDE.md](CLAUDE.md) for the full project contract: scope, security
invariants, plan schema, failure policy, and quality gates.

## Security model (short version)

The app **never custodies user secrets**. The web form accepts only non-secret
identifiers (ticket key/URL, repo identifier). Jira auth comes from environment
variables (`backend/.env`, gitignored — see `backend/.env.example` for the names);
git clone uses your machine's ambient git auth (SSH key / credential helper);
the Anthropic key comes from env. No credential is ever typed into the UI, logged,
sent to the LLM, or committed.

## Prerequisites

- Python ≥ 3.12, Node ≥ 22, git
- [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- [gitleaks](https://github.com/gitleaks/gitleaks) (`winget install Gitleaks.Gitleaks` / `brew install gitleaks`)
- [pre-commit](https://pre-commit.com/) (`pip install pre-commit`), then `pre-commit install`

## Setup

```sh
# backend
cd backend
uv sync
cp .env.example .env   # then fill in values — never commit .env

# frontend
cd frontend
npm install
```

## Run (dev)

```sh
# terminal 1 — backend on :8000
cd backend && uv run uvicorn app.main:app --reload

# terminal 2 — frontend on :3000
cd frontend && npm run dev
```

## Quality gates

Everything must pass before merge. One command, cross-platform:

```sh
python scripts/check.py
```

Runs: pyright, ruff (lint + format), pytest, tsc, eslint, prettier check,
plan-schema drift check, gitleaks. CI (`.github/workflows/ci.yml`) runs the same
script.
