"""Clone step: ambient auth only, shallow clone, typed failures."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import RepoSourceKind
from app.steps.repo_clone import build_clone_command, clone_repo


def make_source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    env_flags = ["-c", "user.email=test@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", "https://github.com/acme/source.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), *env_flags, "commit", "-q", "-m", "init"], check=True)
    return source


def current_branch(path: Path) -> str:
    branch = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return branch.stdout.strip()


@pytest.fixture(autouse=True)
def stable_repo_clone_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.steps.repo_clone.get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[arg-type]
    )


def test_clone_command_is_shallow_and_credential_free() -> None:
    command = build_clone_command("https://github.com/acme/repo", Path("dest"))
    assert command[:2] == ["git", "clone"]
    assert "--depth" in command
    assert "--single-branch" in command
    assert "--no-hardlinks" not in command
    assert "--no-local" not in command
    # no token/credential material anywhere in the command
    joined = " ".join(command)
    assert "@" not in joined.replace("git@", "")  # only the ssh user form may carry @
    assert "http.extraheader" not in joined.lower()


def test_local_clone_command_disables_hardlinks() -> None:
    command = build_clone_command(r"D:\repos\repo", Path("dest"), local_source=True)
    assert "--no-hardlinks" in command
    assert "--no-local" in command


async def test_clone_succeeds_from_local_fixture_repo(tmp_path: Path) -> None:
    source = make_source_repo(tmp_path)
    result = await clone_repo("job1", "PROJ-1", str(source), tmp_path / "workdir")
    dest = result.clone_path
    assert (dest / "hello.py").exists()
    # shallow: exactly one commit
    log = subprocess.run(
        ["git", "-C", str(dest), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"
    assert result.repo_info.source_kind == RepoSourceKind.LOCAL
    assert result.repo_info.branch == current_branch(source)
    assert result.repo_info.origin_url == "https://github.com/acme/source.git"
    assert result.repo_info.is_dirty is False
    assert result.repo_info.local_path == str(source.resolve())


async def test_clone_failure_is_typed_and_detail_safe(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        await clone_repo("job2", "PROJ-2", str(tmp_path / "does-not-exist"), tmp_path / "workdir")
    err = excinfo.value
    assert err.code == ErrorCode.LOCAL_REPO_NOT_FOUND
    assert "does-not-exist" not in err.user_message


async def test_clone_launch_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_run = Mock(side_effect=OSError("subprocess creation failed"))
    monkeypatch.setattr("app.steps.repo_clone.subprocess.run", fake_run)

    with pytest.raises(AppError) as excinfo:
        await clone_repo(
            "job3",
            "PROJ-3",
            "https://github.com/acme/repo",
            tmp_path / "workdir",
        )

    err = excinfo.value
    assert err.code == ErrorCode.CLONE_FAILED
    assert err.internal_detail is not None
    assert "subprocess creation failed" in err.internal_detail


async def test_non_git_local_repo_is_rejected_with_typed_error(tmp_path: Path) -> None:
    source = tmp_path / "plain-folder"
    source.mkdir()

    with pytest.raises(AppError) as excinfo:
        await clone_repo("job4", "PROJ-4", str(source), tmp_path / "workdir")

    assert excinfo.value.code == ErrorCode.LOCAL_REPO_NOT_GIT


async def test_dirty_local_repo_is_rejected_by_default(tmp_path: Path) -> None:
    source = make_source_repo(tmp_path)
    (source / "hello.py").write_text("print('changed')\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        await clone_repo("job_dirty", "PROJ-5", str(source), tmp_path / "workdir")

    assert excinfo.value.code == ErrorCode.LOCAL_REPO_DIRTY


async def test_dirty_local_repo_can_be_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source_repo(tmp_path)
    (source / "hello.py").write_text("print('changed')\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.steps.repo_clone.get_settings",
        lambda: Settings(_env_file=None, allow_dirty_local_repos=True),  # type: ignore[arg-type]
    )

    result = await clone_repo(
        "job_dirty_allowed",
        "PROJ-6",
        str(source),
        tmp_path / "workdir",
    )

    assert result.repo_info.is_dirty is True


async def test_remote_clone_reports_repo_metadata(tmp_path: Path) -> None:
    source = make_source_repo(tmp_path)
    repo_url = (source / ".git").as_uri()
    result = await clone_repo("job5", "PROJ-7", repo_url, tmp_path / "workdir")

    assert result.repo_info.source_kind == RepoSourceKind.REMOTE
    assert result.repo_info.branch == current_branch(source)
    assert result.repo_info.origin_url == repo_url
    assert result.repo_info.is_dirty is False
    assert result.repo_info.local_path is None


async def test_local_repo_branch_mismatch_is_rejected_when_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source_repo(tmp_path)

    monkeypatch.setattr(
        "app.steps.repo_clone.get_settings",
        lambda: Settings(_env_file=None, require_local_branch_ticket_match=True),  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as excinfo:
        await clone_repo("job_branch", "KAN-25", str(source), tmp_path / "workdir")

    assert excinfo.value.code == ErrorCode.LOCAL_REPO_BRANCH_MISMATCH


async def test_local_repo_branch_match_is_accepted_when_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(source), "branch", "-m", "feature/KAN-25-local"],
        check=True,
    )

    monkeypatch.setattr(
        "app.steps.repo_clone.get_settings",
        lambda: Settings(_env_file=None, require_local_branch_ticket_match=True),  # type: ignore[arg-type]
    )

    result = await clone_repo("job_branch_ok", "KAN-25", str(source), tmp_path / "workdir")

    assert result.repo_info.branch == "feature/KAN-25-local"
