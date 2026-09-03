"""Post-implementation validation runner for isolated workspace clones."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
                command=[sys.executable, "-m", "ruff", "check", "."],
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


def _run_command_sync(
    workspace_path: Path,
    command: ValidationCommand,
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
            cwd=workspace_path,
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
            output_excerpt=_truncate_output(combined),
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
            output_excerpt=_truncate_output(combined_output),
        )
    return ValidationResult(
        name=command.name,
        command=" ".join(command.command),
        status=ValidationStatus.FAILED,
        summary=f"Validation command failed with exit code {completed.returncode}.",
        output_excerpt=_truncate_output(combined_output),
    )


async def validate_workspace(workspace_path: Path) -> list[ValidationResult]:
    python_commands = _python_commands(workspace_path)
    node_commands = _node_commands(workspace_path)
    if not python_commands and not node_commands:
        return [
            ValidationResult(
                name="validation-profile",
                command="",
                status=ValidationStatus.SKIPPED,
                summary="No recognized validation profile was detected for this repository.",
            )
        ]

    results: list[ValidationResult] = []
    for command in python_commands:
        results.append(await asyncio.to_thread(_run_command_sync, workspace_path, command))

    if node_commands:
        install_command = npm_install_command(workspace_path)
        if install_command is not None:
            install_result = await asyncio.to_thread(
                _run_command_sync, workspace_path, install_command, _INSTALL_TIMEOUT_SECONDS
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
            results.append(await asyncio.to_thread(_run_command_sync, workspace_path, command))

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
