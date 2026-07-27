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


def _tool_available(command: list[str]) -> bool:
    executable = command[0]
    if executable == sys.executable:
        return True
    return shutil.which(executable) is not None


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


def _run_command_sync(workspace_path: Path, command: ValidationCommand) -> ValidationResult:
    if not _tool_available(command.command):
        return ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.SKIPPED,
            summary="Tool is not available in this environment.",
        )

    logger.info("validation %s: running %s", command.name, command.command)
    try:
        completed = subprocess.run(
            command.command,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
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


def _detect_commands(workspace_path: Path) -> list[ValidationCommand]:
    commands = [*_python_commands(workspace_path), *_node_commands(workspace_path)]
    unique: dict[tuple[str, ...], ValidationCommand] = {}
    for command in commands:
        unique[tuple(command.command)] = command
    return list(unique.values())


async def validate_workspace(workspace_path: Path) -> list[ValidationResult]:
    commands = _detect_commands(workspace_path)
    if not commands:
        return [
            ValidationResult(
                name="validation-profile",
                command="",
                status=ValidationStatus.SKIPPED,
                summary="No recognized validation profile was detected for this repository.",
            )
        ]

    results: list[ValidationResult] = []
    for command in commands:
        results.append(await asyncio.to_thread(_run_command_sync, workspace_path, command))
    return results
