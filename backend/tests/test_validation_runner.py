"""Validation runner: profile detection and structured command results."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.jobs.models import ValidationStatus
from app.steps import validation_runner
from app.steps.validation_runner import ValidationCommand, validate_workspace


async def test_no_profile_returns_skipped_result(tmp_path: Path) -> None:
    results = await validate_workspace(tmp_path)
    assert len(results) == 1
    assert results[0].status == ValidationStatus.SKIPPED
    assert "No recognized validation profile" in results[0].summary


async def test_python_profile_runs_detected_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\nasyncio_mode='auto'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    seen: list[str] = []

    def fake_run(workspace_path: Path, command: ValidationCommand):
        seen.append(command.name)
        assert workspace_path == tmp_path
        return validation_runner.ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.PASSED,
            summary="ok",
        )

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(tmp_path)
    assert [result.name for result in results] == ["ruff", "pytest"]
    assert seen == ["ruff", "pytest"]


async def test_node_profile_runs_detected_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(workspace_path: Path, command: ValidationCommand):
        seen.append(command.name)
        assert workspace_path == tmp_path
        return validation_runner.ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.PASSED,
            summary="ok",
        )

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(tmp_path)
    assert [result.name for result in results] == ["npm lint", "npm test"]
    assert seen == ["npm lint", "npm test"]


async def test_os_launch_failure_becomes_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_detect_commands(workspace_path: Path) -> list[ValidationCommand]:
        del workspace_path
        return [ValidationCommand(name="pytest", command=["missing", "tool"])]

    monkeypatch.setattr(
        validation_runner,
        "_detect_commands",
        fake_detect_commands,
    )

    def raising_run(workspace_path: Path, command: ValidationCommand):
        del workspace_path, command
        raise AppError(ErrorCode.VALIDATION_FAILED, internal_detail="launch failed")

    monkeypatch.setattr(validation_runner, "_run_command_sync", raising_run)
    with pytest.raises(AppError) as excinfo:
        await validate_workspace(tmp_path)
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
