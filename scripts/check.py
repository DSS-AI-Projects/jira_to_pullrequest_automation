#!/usr/bin/env python3
"""Cross-platform quality gate runner — the single source of truth for CI and local.

Usage: python scripts/check.py [--skip-gitleaks]

Sections whose directory does not exist yet are skipped with a warning, so the
gates stay runnable while the milestone is built up commit by commit.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
SCRIPTS_DIR = "Scripts" if sys.platform == "win32" else "bin"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

failures: list[str] = []


def uv() -> list[str]:
    """Locate uv: on PATH, or as a module of this interpreter."""
    if shutil.which("uv"):
        return ["uv"]
    return [sys.executable, "-m", "uv"]


def _backend_venv_script(name: str) -> Path | None:
    script = BACKEND / ".venv" / SCRIPTS_DIR / f"{name}{EXE_SUFFIX}"
    return script if script.exists() else None


def backend_tool(name: str, *args: str) -> list[str]:
    """Use backend/.venv when present; otherwise fall back to uv run."""
    script = _backend_venv_script(name)
    if script is not None:
        return [str(script), *args]
    return [*uv(), "run", name, *args]


def backend_python(*args: str) -> list[str]:
    python = _backend_venv_script("python")
    if python is not None:
        return [str(python), *args]
    return [*uv(), "run", "python", *args]


def npx() -> str:
    return "npx.cmd" if sys.platform == "win32" and shutil.which("npx.cmd") else "npx"


def npm() -> str:
    return "npm.cmd" if sys.platform == "win32" and shutil.which("npm.cmd") else "npm"


def run(name: str, cmd: list[str], cwd: Path = ROOT) -> None:
    print(f"\n=== {name} === ({' '.join(cmd)})", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        failures.append(name)
        print(f"--- FAILED: {name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-gitleaks", action="store_true")
    args = parser.parse_args()

    if BACKEND.exists():
        run("backend: ruff lint", backend_tool("ruff", "check", "."), cwd=BACKEND)
        run(
            "backend: ruff format",
            backend_tool("ruff", "format", "--check", "."),
            cwd=BACKEND,
        )
        run("backend: pyright", backend_tool("pyright"), cwd=BACKEND)
        run("backend: pytest", backend_tool("pytest", "-q"), cwd=BACKEND)
    else:
        print("skip: backend/ not present yet")

    if FRONTEND.exists():
        run("frontend: tsc", [npx(), "tsc", "--noEmit"], cwd=FRONTEND)
        run("frontend: eslint", [npm(), "run", "--silent", "lint"], cwd=FRONTEND)
        run(
            "frontend: prettier",
            [npx(), "prettier", "--check", "src"],
            cwd=FRONTEND,
        )
        run("frontend: vitest", [npm(), "run", "--silent", "test"], cwd=FRONTEND)
    else:
        print("skip: frontend/ not present yet")

    if BACKEND.exists() and FRONTEND.exists():
        # Regenerate the shared plan schema + TS types; any resulting diff means
        # the committed contract has drifted from the Pydantic source of truth.
        run(
            "schema: export json-schema",
            backend_python("-m", "app.schemas.export"),
            cwd=BACKEND,
        )
        run("schema: generate ts", [npm(), "run", "--silent", "gen:plan"], cwd=FRONTEND)
        run(
            "schema: drift check",
            [
                "git",
                "diff",
                "--exit-code",
                "--",
                "schema/plan.schema.json",
                "frontend/src/lib/plan.gen.ts",
            ],
        )

    if args.skip_gitleaks:
        print("skip: gitleaks (--skip-gitleaks)")
    elif shutil.which("gitleaks"):
        # Scan committed history only: the local gitignored .env legitimately
        # holds secrets and must never fail the gate (it must never be committed).
        run("gitleaks: history scan", ["gitleaks", "git", "--redact", "--no-banner"])
    else:
        failures.append("gitleaks: binary not found on PATH")
        print("--- FAILED: gitleaks not installed (see README prerequisites)")

    print()
    if failures:
        print(f"CHECK FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
