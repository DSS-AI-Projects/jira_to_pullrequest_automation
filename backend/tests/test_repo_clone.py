"""Clone step: ambient auth only, shallow clone, typed failures."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.jobs.models import RepoSourceKind
from app.steps.repo_clone import (
    _scrub_origin_url as scrub_origin_url,  # pyright: ignore[reportPrivateUsage]
)
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


def test_scrub_origin_url_strips_embedded_credentials() -> None:
    assert (
        scrub_origin_url("https://j.joshi:glpat-secrettoken@gitlab.example.com/team/repo.git")
        == "https://gitlab.example.com/team/repo.git"
    )
    assert scrub_origin_url("https://user:pass@host:8443/x.git") == "https://host:8443/x.git"


def test_scrub_origin_url_leaves_credential_free_urls_unchanged() -> None:
    assert (
        scrub_origin_url("https://github.com/acme/repo.git") == "https://github.com/acme/repo.git"
    )
    assert scrub_origin_url("git@github.com:acme/repo.git") == "git@github.com:acme/repo.git"
    assert scrub_origin_url(None) is None


async def test_local_clone_scrubs_credential_from_captured_origin_url(tmp_path: Path) -> None:
    source = make_source_repo(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "remote",
            "set-url",
            "origin",
            "https://j.joshi:glpat-secrettoken@gitlab.example.com/team/repo.git",
        ],
        check=True,
    )

    result = await clone_repo("job_cred", "PROJ-8", str(source), tmp_path / "workdir")

    assert result.repo_info.origin_url == "https://gitlab.example.com/team/repo.git"
    assert "glpat-secrettoken" not in (result.repo_info.origin_url or "")


def _with_non_git_folders_allowed() -> Settings:
    return Settings(_env_file=None, allow_local_non_git_folders=True)  # type: ignore[arg-type]


async def test_non_git_folder_is_populated_and_git_initialized_when_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "plain-folder"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")

    monkeypatch.setattr("app.steps.repo_clone.get_settings", _with_non_git_folders_allowed)

    result = await clone_repo("job_folder", "PROJ-10", str(source), tmp_path / "workdir")

    assert (result.clone_path / "app.py").exists()
    assert result.repo_info.source_kind == RepoSourceKind.LOCAL_FOLDER
    assert result.repo_info.branch is None
    assert result.repo_info.origin_url is None
    assert result.repo_info.is_dirty is False
    assert result.repo_info.local_path == str(source.resolve())
    # the workspace copy has its own git history for the implement step's diff
    log = subprocess.run(
        ["git", "-C", str(result.clone_path), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"
    status = subprocess.run(
        ["git", "-C", str(result.clone_path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


async def test_non_git_folder_original_source_is_never_mutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "plain-folder"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")

    monkeypatch.setattr("app.steps.repo_clone.get_settings", _with_non_git_folders_allowed)

    await clone_repo("job_folder_isolation", "PROJ-11", str(source), tmp_path / "workdir")

    assert not (source / ".git").exists()


async def test_empty_non_git_folder_still_produces_a_valid_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "empty-folder"
    source.mkdir()

    monkeypatch.setattr("app.steps.repo_clone.get_settings", _with_non_git_folders_allowed)

    result = await clone_repo("job_folder_empty", "PROJ-12", str(source), tmp_path / "workdir")

    assert result.repo_info.source_kind == RepoSourceKind.LOCAL_FOLDER
    assert result.repo_info.commit_sha


async def test_non_git_folder_copy_excludes_env_shaped_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invariant 3: a plain folder has no .gitignore-enforced protection, so a
    stray secret file must never reach the workspace the agent can read."""
    source = tmp_path / "plain-folder"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=xyz\n", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=xyz\n", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "pkg.js").write_text("// dep\n", encoding="utf-8")

    monkeypatch.setattr("app.steps.repo_clone.get_settings", _with_non_git_folders_allowed)

    result = await clone_repo("job_folder_secrets", "PROJ-13", str(source), tmp_path / "workdir")

    assert (result.clone_path / "app.py").exists()
    assert not (result.clone_path / ".env").exists()
    assert not (result.clone_path / ".env.local").exists()
    assert not (result.clone_path / "node_modules").exists()


async def test_non_git_folder_copy_honors_gitignore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "plain-folder"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (source / "secrets.txt").write_text("shh\n", encoding="utf-8")
    (source / ".gitignore").write_text("secrets.txt\n", encoding="utf-8")

    monkeypatch.setattr("app.steps.repo_clone.get_settings", _with_non_git_folders_allowed)

    result = await clone_repo("job_folder_gitignore", "PROJ-14", str(source), tmp_path / "workdir")

    assert (result.clone_path / "app.py").exists()
    assert not (result.clone_path / "secrets.txt").exists()


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
