"""Best-effort CRLF<->LF normalization wrapped tightly around each
implementation-agent call (the initial implement pass, and any later
validation-correction pass) — never around planning, which only reads/greps
and has no exact-match requirement.

The implementation agent's Edit tool requires an exact literal match between
the `old_string` it composes and the bytes actually on disk. A repo that
stores CRLF line endings (common for a Windows-developed codebase with no
`.gitattributes` text-normalization rule) can make every multi-line edit
silently fail to apply — the model retries against the same file repeatedly,
burns turns/budget, and its final structured summary ends up describing
changes that were never written, while the workspace itself ends up
completely unchanged. Diagnosed from a real job's activity log + recorded
usage: many repeated `Editing X` entries against one CRLF-encoded file, zero
net diff in the workspace afterward — see CLAUDE.md's "Normalizing CRLF line
endings around the implementation agent" note.

`normalize_to_lf()` runs once, right before the implementation agent call,
converting every CRLF text file in the workspace to LF in place and
returning the set of paths it touched (relative to the workspace root).
`restore_original_line_endings()` converts exactly those paths back to CRLF
afterward — including any new content the agent wrote into them — so the
file's original convention is preserved end to end and the diff/branch/
commit the user eventually sees never shows line-ending-only noise. A file
the agent never touched round-trips back to its exact original bytes; a file
the agent did edit gets the same CRLF convention applied uniformly to its
new content too, matching what a developer following that repo's convention
would have produced by hand.

Known limitation: a file with genuinely *mixed* CRLF and bare-LF line endings
(rare, but it happens in real repos) is not restored line-by-line — every
line comes back as CRLF, since normalize_to_lf collapses everything to LF and
restore_original_line_endings can no longer tell which LF was originally a
bare LF. This trades a small amount of diff noise on an already-inconsistent
file for keeping the mechanism simple; a fully line-endings-consistent file
(the overwhelmingly common case) round-trips exactly.

Both functions are synchronous (plain filesystem work) — callers run them via
asyncio.to_thread, matching every other filesystem/subprocess step in this
pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# Not the same list as repo_clone.py's _FOLDER_COPY_ALWAYS_IGNORE (that one's
# a security control for non-git folder copying) — this is purely a
# performance skip-list, kept separate so the two can evolve independently.
_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    "target",
}

_BINARY_SNIFF_BYTES = (
    8000  # matches git's own binary-detection heuristic (a NUL byte anywhere in this prefix)
)


def _is_probably_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _iter_candidate_files(workspace_path: Path, max_file_bytes: int) -> Iterator[Path]:
    for path in workspace_path.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.relative_to(workspace_path).parts):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        yield path


def normalize_to_lf(workspace_path: Path, max_file_bytes: int) -> frozenset[Path]:
    """Convert every CRLF text file under workspace_path to LF in place.
    Returns the set of paths (relative to workspace_path) it converted, for
    restore_original_line_endings() to reverse afterward."""
    converted: set[Path] = set()
    for path in _iter_candidate_files(workspace_path, max_file_bytes):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _is_probably_binary(data[:_BINARY_SNIFF_BYTES]):
            continue
        if b"\r\n" not in data:
            continue
        try:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
        except OSError:
            continue
        converted.add(path.relative_to(workspace_path))
    return frozenset(converted)


def restore_original_line_endings(workspace_path: Path, converted: frozenset[Path]) -> None:
    """Convert exactly the paths normalize_to_lf() reported back to CRLF —
    including any content the implementation agent wrote into them since."""
    for rel_path in converted:
        path = workspace_path / rel_path
        try:
            data = path.read_bytes()
        except OSError:
            continue  # e.g. the file no longer exists — nothing to restore
        # Collapse any \r\n first (idempotent), then expand every \n to
        # \r\n. Handles both untouched content (pure LF from our own
        # normalize_to_lf pass) and any \r\n the agent itself may have
        # written into new content, without ever double-converting into
        # \r\r\n.
        restored = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        if restored != data:
            try:
                path.write_bytes(restored)
            except OSError:
                continue
