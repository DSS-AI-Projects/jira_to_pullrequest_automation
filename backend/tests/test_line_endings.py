"""CRLF<->LF normalization wrapped around the implementation agent call —
see app/steps/line_endings.py's module docstring for the motivating bug."""

from pathlib import Path

from app.steps.line_endings import normalize_to_lf, restore_original_line_endings

MAX_BYTES = 2_000_000


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- normalize_to_lf ---


def test_converts_a_crlf_file_to_lf(tmp_path: Path) -> None:
    write(tmp_path / "a.java", b"line1\r\nline2\r\nline3\r\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    assert (tmp_path / "a.java").read_bytes() == b"line1\nline2\nline3\n"
    assert converted == {Path("a.java")}


def test_leaves_an_already_lf_file_untouched_and_unreported(tmp_path: Path) -> None:
    write(tmp_path / "b.py", b"line1\nline2\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    assert (tmp_path / "b.py").read_bytes() == b"line1\nline2\n"
    assert converted == frozenset()


def test_skips_binary_files(tmp_path: Path) -> None:
    write(tmp_path / "image.bin", b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    # untouched — the NUL byte marks it binary, CRLF bytes inside must survive
    assert (tmp_path / "image.bin").read_bytes() == b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    assert converted == frozenset()


def test_skips_the_git_directory(tmp_path: Path) -> None:
    write(tmp_path / ".git" / "HEAD", b"ref: refs/heads/main\r\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    assert (tmp_path / ".git" / "HEAD").read_bytes() == b"ref: refs/heads/main\r\n"
    assert converted == frozenset()


def test_skips_files_over_the_size_cap(tmp_path: Path) -> None:
    write(tmp_path / "huge.txt", b"x\r\n" * 10)
    converted = normalize_to_lf(tmp_path, max_file_bytes=5)  # smaller than the file
    assert (tmp_path / "huge.txt").read_bytes() == b"x\r\n" * 10
    assert converted == frozenset()


def test_reports_paths_relative_to_the_workspace_root(tmp_path: Path) -> None:
    write(tmp_path / "src" / "nested" / "c.ts", b"x\r\ny\r\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    assert converted == {Path("src") / "nested" / "c.ts"}


# --- restore_original_line_endings ---


def test_restores_an_untouched_file_to_its_exact_original_bytes(tmp_path: Path) -> None:
    original = b"line1\r\nline2\r\nline3\r\n"
    write(tmp_path / "a.java", original)
    converted = normalize_to_lf(tmp_path, MAX_BYTES)

    # simulates the agent reading it but never editing it
    restore_original_line_endings(tmp_path, converted)

    assert (tmp_path / "a.java").read_bytes() == original


def test_restores_agent_written_content_to_crlf_too(tmp_path: Path) -> None:
    write(tmp_path / "a.java", b"old1\r\nold2\r\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)

    # simulates the agent's Edit tool replacing the (now-LF) content
    (tmp_path / "a.java").write_bytes(b"new1\nnew2\nnew3\n")

    restore_original_line_endings(tmp_path, converted)

    assert (tmp_path / "a.java").read_bytes() == b"new1\r\nnew2\r\nnew3\r\n"


def test_is_a_noop_for_paths_not_in_the_converted_set(tmp_path: Path) -> None:
    # A brand new file the agent created — never in the converted set, since
    # it didn't exist when normalize_to_lf ran — is left exactly as written.
    write(tmp_path / "new_file.py", b"created\nby\nagent\n")
    restore_original_line_endings(tmp_path, frozenset())
    assert (tmp_path / "new_file.py").read_bytes() == b"created\nby\nagent\n"


def test_tolerates_a_path_that_no_longer_exists(tmp_path: Path) -> None:
    # Defensive: the implement agent's tool set has no delete capability, but
    # restore must not crash if a tracked path is missing for any reason.
    restore_original_line_endings(tmp_path, frozenset({Path("gone.java")}))  # no exception


def test_round_trips_exactly_when_the_agent_makes_no_changes(tmp_path: Path) -> None:
    original = b"a\r\nb\r\nc\r\n\r\nd\r\n"
    write(tmp_path / "f.java", original)
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    restore_original_line_endings(tmp_path, converted)
    assert (tmp_path / "f.java").read_bytes() == original


def test_mixed_line_endings_normalize_uniformly_to_crlf_on_restore(tmp_path: Path) -> None:
    # Documented, intentional limitation (see module docstring): a file with
    # genuinely mixed CRLF/bare-LF endings loses that per-line distinction —
    # every line comes back as CRLF, not restored line-by-line.
    write(tmp_path / "mixed.java", b"crlf\r\nbarelf\nagain\r\n")
    converted = normalize_to_lf(tmp_path, MAX_BYTES)
    restore_original_line_endings(tmp_path, converted)
    assert (tmp_path / "mixed.java").read_bytes() == b"crlf\r\nbarelf\r\nagain\r\n"
