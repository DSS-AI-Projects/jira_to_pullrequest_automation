"""Validation runner: profile detection and structured command results."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.core import secrets
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import REDACTED
from app.jobs.models import ValidationResult, ValidationStatus
from app.steps import validation_runner
from app.steps.validation_runner import (
    ValidationCommand,
    correctable_failures,
    validate_workspace,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A job workspace laid out like the real one: <job>/repo."""
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _run_dir(workspace: Path) -> Path:
    return workspace.parent / validation_runner.VALIDATION_DIR_NAME


def _passed(command: ValidationCommand) -> ValidationResult:
    return ValidationResult(
        name=command.name,
        command=" ".join(command.command),
        status=ValidationStatus.PASSED,
        summary="ok",
    )


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    monkeypatch.setattr(validation_runner, "get_settings", lambda: settings)


async def test_no_profile_returns_skipped_result(workspace: Path) -> None:
    results = await validate_workspace(workspace)
    assert len(results) == 1
    assert results[0].status == ValidationStatus.SKIPPED
    assert "No recognized validation profile" in results[0].summary


async def test_python_profile_runs_detected_commands(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    (workspace / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\nasyncio_mode='auto'\n",
        encoding="utf-8",
    )
    (workspace / "tests").mkdir()

    seen: list[str] = []

    def fake_run(run_dir: Path, command: ValidationCommand, env: Mapping[str, str]):
        del env
        seen.append(command.name)
        assert run_dir == _run_dir(workspace)
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(workspace)
    assert [result.name for result in results] == ["ruff", "pytest"]
    assert seen == ["ruff", "pytest"]


def test_ruff_runs_in_isolated_mode(workspace: Path) -> None:
    """`python -m` puts the cwd first on sys.path, so a `ruff.py` in the repo
    would run instead of ruff; `-I` keeps the cwd off sys.path."""
    (workspace / "ruff.toml").write_text("", encoding="utf-8")
    [ruff] = validation_runner._python_commands(workspace)  # pyright: ignore[reportPrivateUsage]
    assert ruff.command[1:4] == ["-I", "-m", "ruff"]


async def test_node_profile_runs_detected_commands(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    (workspace / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )
    # Dependencies already installed in the validation copy by an earlier run
    # — the install-gating tests below cover the missing-node_modules path;
    # this one is purely about script detection and run order.
    (_run_dir(workspace) / "node_modules").mkdir(parents=True)

    seen: list[str] = []

    def fake_run(run_dir: Path, command: ValidationCommand, env: Mapping[str, str]):
        del env
        seen.append(command.name)
        assert run_dir == _run_dir(workspace)
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(workspace)
    assert [result.name for result in results] == ["npm lint", "npm test"]
    assert seen == ["npm lint", "npm test"]


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def test_run_command_launches_the_which_resolved_path(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """Regression test: on Windows, `npm`/`npx`/etc. resolve via `shutil.which`
    to a `.cmd`/`.bat` shim. `subprocess.run(["npm", ...])` without `shell=True`
    only auto-appends `.exe` when searching PATH, so launching the bare name
    raises `FileNotFoundError: [WinError 2]` even though `shutil.which` finds
    it. The runner must launch the resolved (extension-included) path instead.
    """
    resolved_path = str(workspace / "npm.cmd")

    def fake_which(executable: str) -> str | None:
        del executable
        return resolved_path

    monkeypatch.setattr(validation_runner.shutil, "which", fake_which)

    captured_argv: list[str] = []

    def fake_subprocess_run(argv: list[str], **kwargs: object) -> _FakeCompleted:
        del kwargs
        captured_argv.extend(argv)
        return _FakeCompleted()

    monkeypatch.setattr(validation_runner.subprocess, "run", fake_subprocess_run)

    (workspace / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (_run_dir(workspace) / "node_modules").mkdir(parents=True)
    results = await validate_workspace(workspace)

    assert captured_argv == [resolved_path, "test"]
    # The displayed command stays the short, readable form.
    assert results[0].command == "npm test"
    assert results[0].status == ValidationStatus.PASSED


async def test_commands_run_in_the_copy_with_the_allowlisted_env(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    monkeypatch.setenv("ACME_DEPLOY_TOKEN", "not-for-validation-0123456789")
    (workspace / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (_run_dir(workspace) / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(validation_runner.shutil, "which", _which_as_is)

    captured: dict[str, object] = {}

    def fake_subprocess_run(argv: list[str], **kwargs: object) -> _FakeCompleted:
        del argv
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(validation_runner.subprocess, "run", fake_subprocess_run)
    await validate_workspace(workspace)

    assert captured["cwd"] == _run_dir(workspace)
    env = captured["env"]
    assert isinstance(env, dict)
    assert "ACME_DEPLOY_TOKEN" not in env
    home = workspace.parent / validation_runner.VALIDATION_HOME_DIR_NAME
    assert env["HOME"] == str(home)
    # The per-run home is gone once validation finishes.
    assert not home.exists()


def test_validation_env_contains_only_allowlisted_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-validationenvcanary")
    monkeypatch.setenv("ACME_DEPLOY_TOKEN", "acme-deploy-canary-0123456789")
    monkeypatch.setenv("SOME_UNRELATED_SETTING", "harmless")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://alice:hunter22@proxy.internal:3128")

    env = validation_runner.validation_env(tmp_path / "home", git_ceiling=tmp_path)

    allowed = (
        set(validation_runner._PASSTHROUGH_ENV)  # pyright: ignore[reportPrivateUsage]
        | set(validation_runner._PROXY_ENV)  # pyright: ignore[reportPrivateUsage]
        | {
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TMPDIR",
            "TMP",
            "TEMP",
            "npm_config_cache",
            "npm_config_userconfig",
            "npm_config_globalconfig",
            "npm_config_update_notifier",
            "npm_config_fund",
            "npm_config_audit",
            "CI",
            "NO_COLOR",
            "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
            "GIT_CEILING_DIRECTORIES",
        }
    )
    assert set(env) <= allowed
    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    # A proxy URL carrying credentials is dropped, not passed through.
    assert "HTTP_PROXY" not in env
    home = tmp_path / "home"
    for key in ("HOME", "TMP", "npm_config_cache", "npm_config_userconfig"):
        assert Path(env[key]).is_relative_to(home)


def test_secret_value_in_env_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "registered-canary-value-0123456789"
    secrets.register_secret(secret)
    # PATH is allowlisted; a secret reaching it must still be refused.
    monkeypatch.setenv("PATH", f"{os.environ.get('PATH', '')}{os.pathsep}{secret}")

    with pytest.raises(AppError) as excinfo:
        validation_runner.validation_env(tmp_path / "home", git_ceiling=tmp_path)
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def test_secret_shaped_key_in_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        validation_runner,
        "_PASSTHROUGH_ENV",
        (*validation_runner._PASSTHROUGH_ENV, "NPM_TOKEN"),  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setenv("NPM_TOKEN", "npm-token-value")

    with pytest.raises(AppError) as excinfo:
        validation_runner.validation_env(tmp_path / "home", git_ceiling=tmp_path)
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def _which_as_is(executable: str) -> str:
    return executable


def _run_with_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str
) -> ValidationResult:
    monkeypatch.setattr(validation_runner.shutil, "which", _which_as_is)

    def failing_with_output(argv: list[str], **kwargs: object) -> _FakeCompleted:
        del argv, kwargs
        return _FakeCompleted(returncode=1, stdout=output)

    monkeypatch.setattr(validation_runner.subprocess, "run", failing_with_output)
    return validation_runner._run_command_sync(  # pyright: ignore[reportPrivateUsage]
        tmp_path, ValidationCommand(name="npm test", command=["npm", "test"]), {}
    )


def test_output_excerpt_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "registered-output-canary-0123456789"
    secrets.register_secret(secret)

    result = _run_with_output(
        monkeypatch, tmp_path, f"env dump: KEY={secret} ANTHROPIC=sk-ant-api03-abcdefghijklmn"
    )

    assert result.output_excerpt is not None
    assert secret not in result.output_excerpt
    assert "sk-ant-api03" not in result.output_excerpt
    assert REDACTED in result.output_excerpt


def test_redaction_happens_before_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    canary = "straddling-canary-value-0123456789"
    secrets.register_secret(canary)
    limit = validation_runner._OUTPUT_LIMIT  # pyright: ignore[reportPrivateUsage]

    # The canary starts 10 characters before the cut: truncating first would
    # keep its first 10 characters, which no longer match the registered value.
    result = _run_with_output(monkeypatch, tmp_path, "x" * (limit - 10) + canary)

    assert result.output_excerpt is not None
    assert canary[:10] not in result.output_excerpt


def test_timeout_output_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "timeout-output-canary-0123456789"
    secrets.register_secret(secret)
    monkeypatch.setattr(validation_runner.shutil, "which", _which_as_is)

    def timing_out(argv: list[str], **kwargs: object) -> _FakeCompleted:
        raise validation_runner.subprocess.TimeoutExpired(argv, 1, output=f"printed {secret}")

    monkeypatch.setattr(validation_runner.subprocess, "run", timing_out)
    result = validation_runner._run_command_sync(  # pyright: ignore[reportPrivateUsage]
        tmp_path, ValidationCommand(name="npm test", command=["npm", "test"]), {}
    )

    assert result.summary == "Validation command timed out."
    assert result.output_excerpt is not None
    assert secret not in result.output_excerpt


def test_validation_copy_excludes_git_and_node_modules(workspace: Path) -> None:
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / "node_modules" / "left-pad").mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.js").write_text("ok", encoding="utf-8")
    run_dir = _run_dir(workspace)

    validation_runner.prepare_validation_copy(workspace, run_dir)

    assert (run_dir / "src" / "app.js").read_text(encoding="utf-8") == "ok"
    assert not (run_dir / ".git").exists()
    assert not (run_dir / "node_modules").exists()


def test_validation_copy_keeps_its_own_node_modules_and_drops_stale_files(
    workspace: Path,
) -> None:
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    run_dir = _run_dir(workspace)
    validation_runner.prepare_validation_copy(workspace, run_dir)
    (run_dir / "node_modules" / "vitest").mkdir(parents=True)
    (run_dir / "coverage.xml").write_text("left by the previous run", encoding="utf-8")
    (workspace / "package.json").write_text('{"changed": true}', encoding="utf-8")

    validation_runner.prepare_validation_copy(workspace, run_dir)

    assert (run_dir / "node_modules" / "vitest").is_dir()
    assert not (run_dir / "coverage.xml").exists()
    assert (run_dir / "package.json").read_text(encoding="utf-8") == '{"changed": true}'


async def test_nothing_is_copied_back_to_the_workspace(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    (workspace / ".git").mkdir()
    (workspace / "ruff.toml").write_text("", encoding="utf-8")

    def writing_run(run_dir: Path, command: ValidationCommand, env: Mapping[str, str]):
        del env
        (run_dir / "coverage.xml").write_text("artifact", encoding="utf-8")
        (run_dir / ".git" / "hooks").mkdir(parents=True)
        (run_dir / ".git" / "hooks" / "pre-commit").write_text("evil", encoding="utf-8")
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", writing_run)
    await validate_workspace(workspace)

    assert sorted(path.name for path in workspace.iterdir()) == [".git", "ruff.toml"]
    assert not any((workspace / ".git").iterdir())


async def test_validation_disabled_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _use_settings(monkeypatch, validation_enabled=False)
    (workspace / "ruff.toml").write_text("", encoding="utf-8")

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(validation_runner.subprocess, "run", must_not_run)
    results = await validate_workspace(workspace)

    assert [result.status for result in results] == [ValidationStatus.SKIPPED]
    assert results[0].summary == "Validation is disabled on this deployment."
    assert correctable_failures(results) == []
    assert not _run_dir(workspace).exists()


async def test_shared_deployment_does_not_run_unsandboxed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _use_settings(monkeypatch, auth_trusted_proxy_enabled=True)
    (workspace / "ruff.toml").write_text("", encoding="utf-8")

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(validation_runner.subprocess, "run", must_not_run)
    results = await validate_workspace(workspace)

    assert [result.status for result in results] == [ValidationStatus.SKIPPED]
    assert "without a sandbox" in results[0].summary


async def test_shared_deployment_can_explicitly_allow_unsandboxed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _use_settings(monkeypatch, auth_trusted_proxy_enabled=True, validation_allow_unsandboxed=True)
    (workspace / "ruff.toml").write_text("", encoding="utf-8")

    def fake_run(run_dir: Path, command: ValidationCommand, env: Mapping[str, str]):
        del run_dir, env
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(workspace)

    assert [result.name for result in results] == ["ruff"]


async def test_os_launch_failure_becomes_typed_error(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    def fake_python_commands(workspace_path: Path) -> list[ValidationCommand]:
        del workspace_path
        return [ValidationCommand(name="pytest", command=["missing", "tool"])]

    monkeypatch.setattr(
        validation_runner,
        "_python_commands",
        fake_python_commands,
    )

    def raising_run(
        run_dir: Path, command: ValidationCommand, env: Mapping[str, str], timeout: int = 120
    ):
        del run_dir, command, env, timeout
        raise AppError(ErrorCode.VALIDATION_FAILED, internal_detail="launch failed")

    monkeypatch.setattr(validation_runner, "_run_command_sync", raising_run)
    with pytest.raises(AppError) as excinfo:
        await validate_workspace(workspace)
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
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    (workspace / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(
        run_dir: Path, command: ValidationCommand, env: Mapping[str, str], timeout: int = 120
    ):
        del env, timeout
        seen.append(command.name)
        assert run_dir == _run_dir(workspace)
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(workspace)
    assert [result.name for result in results] == ["npm install", "npm lint", "npm test"]
    assert seen == ["npm install", "npm lint", "npm test"]


async def test_workspace_node_modules_does_not_skip_the_install(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """node_modules is never copied into the validation copy, so one sitting
    in the workspace doesn't count as installed."""
    (workspace / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (workspace / "node_modules").mkdir()

    seen: list[str] = []

    def fake_run(
        run_dir: Path, command: ValidationCommand, env: Mapping[str, str], timeout: int = 120
    ):
        del run_dir, env, timeout
        seen.append(command.name)
        return _passed(command)

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    await validate_workspace(workspace)
    assert seen == ["npm install", "npm test"]


async def test_failed_npm_install_skips_remaining_node_commands(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    (workspace / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","test":"vitest run"}}', encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(
        run_dir: Path, command: ValidationCommand, env: Mapping[str, str], timeout: int = 120
    ):
        del run_dir, env, timeout
        seen.append(command.name)
        return validation_runner.ValidationResult(
            name=command.name,
            command=" ".join(command.command),
            status=ValidationStatus.FAILED,
            summary="Validation command failed with exit code 1.",
        )

    monkeypatch.setattr(validation_runner, "_run_command_sync", fake_run)
    results = await validate_workspace(workspace)

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
