"""Linux-only hardening of the backend process itself.

Validation runs the target repo's code as a child process under the same uid
as the backend. An allowlisted child environment doesn't help if the child can
just read the backend's own: `/proc/<pid>/environ` keeps the environment the
process started with (in the container, every value from backend/.env), and
same-uid processes may read it, or attach with ptrace.

`PR_SET_DUMPABLE=0` makes this process's `/proc/<pid>/*` files root-owned and
refuses same-uid ptrace, so a non-root child can read neither. It is a partial
measure (docs/validation-isolation-proposal.md §3.5): the child still shares
the backend's filesystem and network.
"""

from __future__ import annotations

import ctypes
import sys

from app.core.logging import get_logger

logger = get_logger(__name__)

_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3


def make_process_non_dumpable() -> bool:
    """Mark this process non-dumpable. Returns True if it now is; a no-op that
    returns False on other platforms or if the call fails."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.warning("prctl(PR_SET_DUMPABLE) failed: errno %s", ctypes.get_errno())
            return False
        return libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError) as exc:
        logger.warning("prctl unavailable: %s", type(exc).__name__)
        return False
