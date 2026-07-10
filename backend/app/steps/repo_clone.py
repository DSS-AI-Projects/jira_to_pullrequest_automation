"""Job step (b): clone the repo. Deterministic, no LLM.

Auth model: the subprocess inherits the machine's ambient git auth (SSH agent,
credential helper). The app never constructs, reads, or injects a credential;
GIT_TERMINAL_PROMPT=0 makes git fail fast instead of prompting, so a missing
credential becomes a typed CLONE_FAILED rather than a hang.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact

logger = get_logger(__name__)


def build_clone_command(repo_url: str, dest: Path) -> list[str]:
    """Pure command builder (tested): shallow, single-branch, no credentials."""
    return [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--",
        repo_url,
        str(dest),
    ]


async def clone_repo(job_id: str, repo_url: str, workdir: Path) -> Path:
    settings = get_settings()
    dest = workdir / job_id / "repo"
    dest.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    command = build_clone_command(repo_url, dest)
    logger.info("job %s: cloning %s", job_id, repo_url)

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.clone_timeout_seconds
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise AppError(
            ErrorCode.CLONE_FAILED,
            internal_detail=f"clone timed out after {settings.clone_timeout_seconds}s",
        ) from exc

    if process.returncode != 0:
        detail = redact(stderr.decode(errors="replace"))[:2000]
        raise AppError(ErrorCode.CLONE_FAILED, internal_detail=detail)

    logger.info("job %s: clone complete", job_id)
    return dest
