"""Validation runner: profile detection and structured command results."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.jobs.models import ValidationResult, ValidationStatus
from app.steps import validation_runner
from app.steps.validation_runner import (
    ValidationCommand,
    correctable_failures,
    validate_workspace,
)


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
    # Dependencies already installed — the install-gating tests below cover
    # the missing-node_modules path; this one is purely about script
    # detection and run order.
    (tmp_path / "node_modules").mkdir()

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


async def test_run_command_launches_the_which_resolved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression test: on Windows, `npm`/`npx`/etc. resolve via `shutil.which`
    to a `.cmd`/`.bat` shim. `subprocess.run(["npm", ...])` without `shell=True`
    only auto-appends `.exe` when searching PATH, so launching the bare name
    raises `FileNotFoundError: [WinError 2]` even though `shutil.which` finds
    it. The runner must launch the resolved (extension-included) path instead.
    """
    resolved_path = str(tmp_path / "npm.cmd")

    def fake_which(executable: str) -> str | None:
        del executable
        return resolved_path

    monkeypatch.setattr(validation_runner.shutil, "which", fake_which)

    captured_argv: list[str] = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(argv: list[str], **kwargs: object) -> FakeCompleted:
        del kwargs
        captured_argv.extend(argv)
        return FakeCompleted()

    monkeypatch.setattr(validation_runner.subprocess, "run", fake_subprocess_run)

    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    results = await validate_workspace(tmp_path)

    assert captured_argv == [resolved_path, "test"]
    # The displayed command stays the short, readable form.
    assert results[0].command == "npm test"
    assert results[0].status == ValidationStatus.PASSED


async def test_os_launch_failure_becomes_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_python_commands(workspace_path: Path) -> list[ValidationCommand]:
        del workspace_path
        return [ValidationCommand(name="pytest", command=["missing", "tool"])]

    monkeypatch.setattr(
        validation_runner,
        "_python_commands",
        fake_python_commands,
    )

    def raising_run(workspace_path: Path, command: ValidationCommand, timeout: int = 120):
        del workspace_path, command, timeout
        raise AppError(ErrorCode.VALIDATION_FAILED, internal_detail="launch failed")

    monkeypatch.setattr(validation_runner, "_run_command_sync", raising_run)
    with pytest.raises(AppError) as excinfo:
        await validate_workspace(tmp_path)
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def test_npm_install_command_prefers_ci_when_lockfile_present(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{}}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    command = validation_runner.npm_install_command(tmp_path)

    assert command is not None
    assert command.command == ["npm", "ci"]


def test_npm_install_command_falls_back_to_install_without_lockfile(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{}}', encoding="utf-8")

    command = validation_runner.npm_install_command(tmp_path)

    assert command is not None
    assert command.command == ["npm", "install"]


def test_npm_install_command_is_none_when_dependencies_already_present(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()

    assert validation_runner.npm_install_command(tmp_path) is None


def test_npm_install_command_is_none_without_package_json(tmp_path: Path) -> None:
    assert validation_runner.npm_install_command(tmp_path) is None


async def test_npm_install_runs_before_node_commands_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(workspace_path: Path, command: ValidationCommand, timeout: int = 120):
        del timeout
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
    assert [result.name for result in results] == ["npm install", "npm lint", "npm test"]
    assert seen == ["npm install", "npm lint", "npm test"]


async def test_failed_npm_install_skips_remaining_node_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(workspace_path: Path, command: ValidationCommand, timeout: int = 120):
        del workspace_path, timeout
        seen.append(command.name)
        return validation_runner.ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.FAILED,
            summary="Validation command failed with exit code 1.",
        )

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(tmp_path)

    # The install step actually ran and failed; lint/test were skipped
    # without ever being launched (no point burning another timeout each
    # for a failure that's already explained).
    assert seen == ["npm install"]
    assert [result.name for result in results] == ["npm install", "npm lint", "npm test"]
    assert results[0].status == ValidationStatus.FAILED
    assert results[1].status == ValidationStatus.SKIPPED
    assert results[2].status == ValidationStatus.SKIPPED
    assert "did not succeed" in results[1].summary


def test_correctable_failures_excludes_a_failed_npm_install() -> None:
    results = [
        ValidationResult(
            name="npm install",
            command="npm install",
            status=ValidationStatus.FAILED,
            summary="Validation command failed with exit code 1.",
        ),
        ValidationResult(
            name="npm lint",
            command="npm run lint",
            status=ValidationStatus.SKIPPED,
            summary="Skipped because installing dependencies (npm install) did not succeed.",
        ),
        ValidationResult(
            name="ruff",
            command="ruff check .",
            status=ValidationStatus.FAILED,
            summary="Validation command failed with exit code 1.",
        ),
    ]

    failures = correctable_failures(results)

    assert [result.name for result in failures] == ["ruff"]


def test_correctable_failures_returns_empty_when_nothing_failed() -> None:
    results = [
        ValidationResult(
            name="ruff",
            command="ruff check .",
            status=ValidationStatus.PASSED,
            summary="Validation command passed.",
        ),
    ]

    assert correctable_failures(results) == []
