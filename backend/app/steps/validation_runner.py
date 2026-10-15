"""Post-implementation validation runner for isolated workspace clones.

This is the only place the app executes the target repo's own code (npm
lifecycle scripts, `npm run lint`/`npm test`, `pytest`), so it is treated as
hostile (security invariant 6, docs/validation-isolation-proposal.md):

- Commands run on a copy of the working tree without `.git`
  (`<job>/validation/`), and nothing is copied back — validation can't plant
  git hooks or slip build/test artifacts into the reviewed commit.
- The child gets an allowlisted environment (`validation_env`) with a fresh
  per-run home (`<job>/validation-home/`), never the backend's own env.
- Output is redacted before it is stored, shown, or quoted to the agent.
- On a shared deployment (trusted-proxy auth) nothing runs unless explicitly
  allowed: this is still a plain subprocess under the backend's own uid, not a
  sandbox — that's phase 2 of the proposal, not built yet.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.core import secrets
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact
from app.jobs.models import ValidationResult, ValidationStatus

logger = get_logger(__name__)

_OUTPUT_LIMIT = 1200
_COMMAND_TIMEOUT_SECONDS = 120
# npm install can be much slower than a lint/test run, especially cold
# (no local npm cache yet in a fresh workspace) — give it more room.
_INSTALL_TIMEOUT_SECONDS = 300

# The name every dependency-install ValidationResult carries. Exported so
# callers (the validation-correction endpoint/runner) can exclude it from
# "failures worth asking the implementation agent to fix" — no source edit
# can resolve a failed `npm install`, so feeding it to the corrective agent
# would just burn its one-shot budget on an unfixable prompt.
INSTALL_STEP_NAME = "npm install"

# Per-job directories next to the workspace (`<workdir>/<job>/repo`), so
# workspace retention removes them together with the job directory.
VALIDATION_DIR_NAME = "validation"
VALIDATION_HOME_DIR_NAME = "validation-home"

# Never copied into the validation copy: `.git` so validation can't touch the
# workspace's hooks/config (create-branch and push run git there later), and
# `node_modules` because the copy keeps its own install between runs.
_COPY_EXCLUDED_NAMES = frozenset({".git", "node_modules"})

# Environment the validation child may inherit from the backend. An allowlist,
# not a denylist: anything not named here — every secret included — is absent.
_PASSTHROUGH_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    # Windows: Python/Node fail without these; .cmd shims need COMSPEC/PATHEXT.
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    # Corporate TLS (the backend image sets SSL_CERT_FILE for the internal CA).
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
)
# Passed only when the URL carries no userinfo (http://user:pass@proxy).
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
_SECRET_SHAPED_KEY_RE = re.compile(r"(?i)token|secret|key|password|credential")

_DISABLED_SUMMARY = "Validation is disabled on this deployment."
_UNSANDBOXED_SUMMARY = (
    "Validation is not run on this shared deployment: it would execute the "
    "repository's own code without a sandbox."
)


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    command: list[str]


def _read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _python_commands(workspace_path: Path) -> list[ValidationCommand]:
    commands: list[ValidationCommand] = []
    pyproject = workspace_path / "pyproject.toml"
    pyproject_text = _read_text(pyproject) if pyproject.exists() else ""

    if "[tool.ruff]" in pyproject_text or (workspace_path / "ruff.toml").exists():
        commands.append(
            ValidationCommand(
                name="ruff",
                # -I: the repo's root isn't put on sys.path, so a `ruff.py`
                # in the repo can't shadow ruff and run as code.
                command=[sys.executable, "-I", "-m", "ruff", "check", "."],
            )
        )

    has_pytest_config = (
        any(
            path.exists()
            for path in (
                workspace_path / "pytest.ini",
                workspace_path / "tox.ini",
                workspace_path / "setup.cfg",
            )
        )
        or "[tool.pytest.ini_options]" in pyproject_text
    )
    has_tests = (workspace_path / "tests").exists()
    if has_pytest_config or has_tests:
        commands.append(
            ValidationCommand(
                name="pytest",
                command=[sys.executable, "-m", "pytest", "-q"],
            )
        )
    return commands


def _node_commands(workspace_path: Path) -> list[ValidationCommand]:
    package_json = workspace_path / "package.json"
    if not package_json.exists():
        return []
    data = _read_json(package_json)
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []

    commands: list[ValidationCommand] = []
    if "lint" in scripts:
        commands.append(ValidationCommand(name="npm lint", command=["npm", "run", "lint"]))
    if "test" in scripts:
        commands.append(ValidationCommand(name="npm test", command=["npm", "test"]))
    return commands


def npm_install_command(workspace_path: Path) -> ValidationCommand | None:
    """An `npm ci`/`npm install` step to run before npm-based validation
    commands, if the workspace's dependencies aren't already installed.

    The isolated workspace clone is just the repo's git tree — `node_modules`
    is never tracked by git, so it's never present in a fresh clone. Without
    installing first, any npm-based validation command that relies on a
    devDependency binary (ng, eslint, vitest, ...) fails with "not
    recognized" / "command not found" every time — not a real code defect.
    Prefer `npm ci` (deterministic, matches the lockfile exactly) when a
    lockfile is present; fall back to `npm install` otherwise. Returns None
    when there's nothing to install (no package.json) or dependencies are
    already present (e.g. a later validation pass in the same workspace,
    such as the post-correction revalidation).
    """
    package_json = workspace_path / "package.json"
    if not package_json.exists():
        return None
    if (workspace_path / "node_modules").exists():
        return None
    if (workspace_path / "package-lock.json").exists():
        return ValidationCommand(name=INSTALL_STEP_NAME, command=["npm", "ci"])
    return ValidationCommand(name=INSTALL_STEP_NAME, command=["npm", "install"])


def _resolve_executable(executable: str) -> str | None:
    """Resolve a command's executable to the path `subprocess.run` can launch.

    On Windows, tools installed as shims (npm, npx, yarn, pnpm, ...) resolve to a
    `.cmd`/`.bat` file. `subprocess.run` without `shell=True` only appends `.exe`
    when searching PATH for a bare name, so passing e.g. "npm" directly raises
    `FileNotFoundError: [WinError 2] The system cannot find the file specified`
    even though `shutil.which("npm")` finds it. Resolving to the full path
    (extension included) fixes this: Windows launches `.cmd`/`.bat` files fine
    when given the full path, no shell wrapper needed.
    """
    if executable == sys.executable:
        return executable
    return shutil.which(executable)


def _tool_available(command: list[str]) -> bool:
    return _resolve_executable(command[0]) is not None


def _truncate_output(output: str) -> str | None:
    text = output.strip()
    if not text:
        return None
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[:_OUTPUT_LIMIT] + "...(truncated)"


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def validation_env(run_home: Path, git_ceiling: Path) -> dict[str, str]:
    """The complete environment for a validation command: allowlisted
    pass-throughs plus a per-run home, temp dir and npm config. Nothing else
    from the backend's environment reaches the child.

    `git_ceiling` is the job directory. The validation copy has no `.git`, so
    without a ceiling git would walk up from it into whatever repository
    encloses the workdir — in local dev, this app's own checkout, where a
    husky `prepare` (`git config core.hooksPath .husky`) would land."""
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV if os.environ.get(key)}
    for key in _PROXY_ENV:
        value = os.environ.get(key)
        if value and "@" not in value:
            env[key] = value
    tmp = run_home / "tmp"
    env.update(
        {
            # A fresh home: no ~/.npmrc (registry tokens), ~/.git-credentials,
            # ~/.config/gh or ~/.claude of the user the backend runs as.
            "HOME": str(run_home),
            "USERPROFILE": str(run_home),
            "APPDATA": str(run_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(run_home / "AppData" / "Local"),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            # Per-run cache: nothing shared between jobs (an unpinned
            # `npm install` cache entry could otherwise be poisoned for the next).
            "npm_config_cache": str(run_home / "npm-cache"),
            "npm_config_userconfig": str(run_home / ".npmrc"),
            "npm_config_globalconfig": str(run_home / "npmrc-global"),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            # Test runners: no watch mode, no prompts.
            "CI": "true",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CEILING_DIRECTORIES": str(git_ceiling),
        }
    )
    _assert_no_secret_values(env)
    return env


def _assert_no_secret_values(env: Mapping[str, str]) -> None:
    """Fail closed if a secret could reach the child: a guard against a future
    edit adding the wrong variable to the allowlist above."""
    registered = secrets.registered_secrets()
    for key, value in env.items():
        if _SECRET_SHAPED_KEY_RE.search(key) or any(secret in value for secret in registered):
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                internal_detail=f"validation env rejected: {key} looks secret-bearing",
            )


def _make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    # Read-only files (git objects, some npm packages) block rmtree on Windows.
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, onexc=_make_writable_and_retry)


def _remove_tree_best_effort(path: Path) -> None:
    try:
        _remove_tree(path)
    except OSError:
        logger.warning("could not remove validation directory %s", path)


def prepare_validation_copy(workspace_path: Path, run_dir: Path) -> None:
    """Make `run_dir` a copy of the workspace's working tree, without `.git`.

    A previous copy's `node_modules` is kept, so the post-correction
    revalidation doesn't reinstall; everything else is replaced, so files the
    previous run created don't linger. Symlinks are copied as links, never
    followed, so a repo symlink can't pull an outside file into the copy.
    Nothing is ever copied back to the workspace.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    for entry in run_dir.iterdir():
        if entry.name != "node_modules":
            _remove_tree(entry)

    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _COPY_EXCLUDED_NAMES}

    shutil.copytree(workspace_path, run_dir, ignore=_ignore, symlinks=True, dirs_exist_ok=True)


def _prepare_run_home(run_home: Path) -> None:
    _remove_tree(run_home)
    for directory in (
        run_home / "tmp",
        run_home / "AppData" / "Roaming",
        run_home / "AppData" / "Local",
        run_home / "npm-cache",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (run_home / ".npmrc").write_text("", encoding="utf-8")
    (run_home / "npmrc-global").write_text("", encoding="utf-8")


def _run_command_sync(
    run_dir: Path,
    command: ValidationCommand,
    env: Mapping[str, str],
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
) -> ValidationResult:
    resolved_executable = _resolve_executable(command.command[0])
    if resolved_executable is None:
        return ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.SKIPPED,
            summary="Tool is not available in this environment.",
        )
    argv = [resolved_executable, *command.command[1:]]

    logger.info("validation %s: running %s", command.name, command.command)
    try:
        completed = subprocess.run(
            argv,
            cwd=run_dir,
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            internal_detail=redact(f"{command.name} launch failed: {type(exc).__name__}: {exc}"),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_output(cast(str | bytes | None, exc.stdout))
        stderr = _coerce_output(cast(str | bytes | None, exc.stderr))
        combined = stdout + (f"\n{stderr}" if stderr else "")
        return ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.FAILED,
            summary="Validation command timed out.",
            output_excerpt=_safe_excerpt(combined),
        )

    combined_output = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    if completed.returncode == 0:
        return ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.PASSED,
            summary="Validation command passed.",
            output_excerpt=_safe_excerpt(combined_output),
        )
    return ValidationResult(
        name=command.name,
        command=" ".join(command.command),
        status=ValidationStatus.FAILED,
        summary=f"Validation command failed with exit code {completed.returncode}.",
        output_excerpt=_safe_excerpt(combined_output),
    )


def _safe_excerpt(output: str) -> str | None:
    # Redact before truncating, so a secret straddling the cut can't leak a
    # prefix the redactor no longer recognizes.
    return _truncate_output(redact(output))


def _skipped_profile(summary: str) -> list[ValidationResult]:
    return [
        ValidationResult(
            name="validation-profile",
            command="",
            status=ValidationStatus.SKIPPED,
            summary=summary,
        )
    ]


def _blocked_reason(settings: Settings) -> str | None:
    if not settings.validation_enabled:
        return _DISABLED_SUMMARY
    # Trusted-proxy auth marks a shared deployment; there an unsandboxed run
    # would expose every user's data to one repo's code. Fail safe.
    if settings.auth_trusted_proxy_enabled and not settings.validation_allow_unsandboxed:
        return _UNSANDBOXED_SUMMARY
    return None


async def validate_workspace(workspace_path: Path) -> list[ValidationResult]:
    blocked = _blocked_reason(get_settings())
    if blocked is not None:
        return _skipped_profile(blocked)

    python_commands = _python_commands(workspace_path)
    node_commands = _node_commands(workspace_path)
    if not python_commands and not node_commands:
        return _skipped_profile(
            "No recognized validation profile was detected for this repository."
        )

    # Every configured secret must be known to redact() before any output is
    # captured; registration is otherwise lazy (on first read).
    secrets.register_configured_secrets()

    job_dir = workspace_path.parent
    run_dir = job_dir / VALIDATION_DIR_NAME
    run_home = job_dir / VALIDATION_HOME_DIR_NAME
    try:
        await asyncio.to_thread(prepare_validation_copy, workspace_path, run_dir)
        await asyncio.to_thread(_prepare_run_home, run_home)
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            internal_detail=redact(f"validation copy failed: {type(exc).__name__}: {exc}"),
        ) from exc
    try:
        env = validation_env(run_home, git_ceiling=job_dir)
        return await _run_profiles(run_dir, env, python_commands, node_commands)
    finally:
        await asyncio.to_thread(_remove_tree_best_effort, run_home)


async def _run_profiles(
    run_dir: Path,
    env: Mapping[str, str],
    python_commands: list[ValidationCommand],
    node_commands: list[ValidationCommand],
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for command in python_commands:
        results.append(await asyncio.to_thread(_run_command_sync, run_dir, command, env))

    if node_commands:
        install_command = npm_install_command(run_dir)
        if install_command is not None:
            install_result = await asyncio.to_thread(
                _run_command_sync, run_dir, install_command, env, _INSTALL_TIMEOUT_SECONDS
            )
            results.append(install_result)
            if install_result.status != ValidationStatus.PASSED:
                # Dependencies aren't installed, so every npm-based command
                # below would just fail the same way for the same reason —
                # skip them instead of burning another timeout each.
                results.extend(
                    ValidationResult(
                        name=command.name,
                        command=" ".join(command.command),
                        status=ValidationStatus.SKIPPED,
                        summary=(
                            "Skipped because installing dependencies (npm install) did not succeed."
                        ),
                    )
                    for command in node_commands
                )
                return results
        for command in node_commands:
            results.append(await asyncio.to_thread(_run_command_sync, run_dir, command, env))

    return results


def correctable_failures(results: list[ValidationResult]) -> list[ValidationResult]:
    """Failed results worth handing to the implementation agent to fix.

    Excludes a failed `npm install` step: it's an environment/dependency
    problem, not a code defect, so no source edit could ever satisfy it —
    offering it up would just spend the one-shot correction attempt on a
    prompt the agent can't succeed at.
    """
    return [
        result
        for result in results
        if result.status == ValidationStatus.FAILED and result.name != INSTALL_STEP_NAME
    ]
