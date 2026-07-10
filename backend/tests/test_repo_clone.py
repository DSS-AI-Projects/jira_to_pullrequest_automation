"""Clone step: ambient auth only, shallow clone, typed failures."""

import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.steps.repo_clone import build_clone_command, clone_repo


def make_source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    env_flags = ["-c", "user.email=test@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), *env_flags, "commit", "-q", "-m", "init"], check=True)
    return source


def test_clone_command_is_shallow_and_credential_free() -> None:
    command = build_clone_command("https://github.com/acme/repo", Path("dest"))
    assert command[:2] == ["git", "clone"]
    assert "--depth" in command
    assert "--single-branch" in command
    # no token/credential material anywhere in the command
    joined = " ".join(command)
    assert "@" not in joined.replace("git@", "")  # only the ssh user form may carry @
    assert "http.extraheader" not in joined.lower()


async def test_clone_succeeds_from_local_fixture_repo(tmp_path: Path) -> None:
    source = make_source_repo(tmp_path)
    dest = await clone_repo("job1", str(source), tmp_path / "workdir")
    assert (dest / "hello.py").exists()
    # shallow: exactly one commit
    log = subprocess.run(
        ["git", "-C", str(dest), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"


async def test_clone_failure_is_typed_and_detail_safe(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        await clone_repo("job2", str(tmp_path / "does-not-exist"), tmp_path / "workdir")
    err = excinfo.value
    assert err.code == ErrorCode.CLONE_FAILED
    assert err.internal_detail  # captured for the (redacted) server log
    # the user-facing message is the safe catalog text, not git stderr
    assert "does-not-exist" not in err.user_message
