"""Input validation — security invariants 1 and 5 (backend half)."""

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.schemas.inputs import (
    JOB_CREATE_FORM_FIELDS,
    normalize_document_ticket_key,
    normalize_mantis_issue,
    normalize_planning_notes,
    normalize_repo,
    normalize_requirement_text,
    normalize_requirement_title,
    normalize_ticket,
    reject_unknown_form_fields,
    require_exactly_one_requirement_source,
    validate_uploaded_document,
)

FAKE_TOKEN = "ATATT" + "3xF" + "a" * 27


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    repos_file = tmp_path / "repos.config.json"
    if not repos_file.exists():
        repos_file.write_text(
            json.dumps(
                {"repos": [{"name": "my-service", "url": "https://github.com/acme/my-service"}]}
            ),
            encoding="utf-8",
        )
    defaults: dict[str, object] = {"preconfigured_repos_file": repos_file}
    return Settings(_env_file=None, **{**defaults, **overrides})  # type: ignore[arg-type]


# --- ticket ---


def test_plain_key_is_accepted_and_uppercased(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert normalize_ticket("proj-123", settings) == "PROJ-123"
    assert normalize_ticket("  ABC2-9  ", settings) == "ABC2-9"


def test_browse_url_is_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, jira_base_url="https://acme.atlassian.net")
    key = normalize_ticket("https://acme.atlassian.net/browse/PROJ-123", settings)
    assert key == "PROJ-123"


def test_board_url_with_selected_issue_is_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, jira_base_url="https://acme.atlassian.net")
    url = "https://acme.atlassian.net/jira/software/c/projects/P/boards/1?selectedIssue=PROJ-7"
    assert normalize_ticket(url, settings) == "PROJ-7"


def test_ticket_url_on_wrong_host_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, jira_base_url="https://acme.atlassian.net")
    with pytest.raises(AppError) as excinfo:
        normalize_ticket("https://evil.example.com/browse/PROJ-1", settings)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_garbage_ticket_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_ticket("not a ticket", make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_token_shaped_ticket_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_ticket(FAKE_TOKEN, make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert FAKE_TOKEN not in excinfo.value.user_message


# --- document_ticket_key (optional, alongside an uploaded PDF) ---


def test_document_ticket_key_none_stays_none(tmp_path: Path) -> None:
    assert normalize_document_ticket_key(None, make_settings(tmp_path)) is None


def test_document_ticket_key_blank_normalizes_to_none(tmp_path: Path) -> None:
    assert normalize_document_ticket_key("   ", make_settings(tmp_path)) is None


def test_document_ticket_key_plain_key_is_accepted_and_uppercased(tmp_path: Path) -> None:
    assert normalize_document_ticket_key("kan-31", make_settings(tmp_path)) == "KAN-31"


def test_document_ticket_key_accepts_a_ticket_url_too(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, jira_base_url="https://acme.atlassian.net")
    key = normalize_document_ticket_key("https://acme.atlassian.net/browse/KAN-31", settings)
    assert key == "KAN-31"


def test_document_ticket_key_garbage_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_document_ticket_key("not a ticket", make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_document_ticket_key_token_shaped_is_rejected_without_echoing_value(
    tmp_path: Path,
) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_document_ticket_key(FAKE_TOKEN, make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert FAKE_TOKEN not in excinfo.value.user_message


# --- repo ---


def test_preconfigured_name_resolves_to_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert normalize_repo("my-service", settings) == "https://github.com/acme/my-service"


def test_https_url_on_allowed_host_is_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    url = "https://github.com/acme/other-repo.git"
    assert normalize_repo(url, settings) == url


def test_scp_like_ssh_url_is_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert (
        normalize_repo("git@github.com:acme/repo.git", settings) == "git@github.com:acme/repo.git"
    )


def test_disallowed_host_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(AppError) as excinfo:
        normalize_repo("https://gitlab.com/acme/repo", settings)
    assert excinfo.value.code == ErrorCode.REPO_HOST_NOT_ALLOWED


def test_extra_allowed_host_can_be_configured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allowed_git_hosts=["github.com", "gitlab.com"])
    assert normalize_repo("https://gitlab.com/acme/repo", settings)


def test_allowed_local_path_is_accepted(tmp_path: Path) -> None:
    local_root = tmp_path / "repos"
    local_repo = local_root / "my-service"
    local_repo.mkdir(parents=True)
    settings = make_settings(
        tmp_path,
        allow_local_repos=True,
        allowed_local_repo_roots=[local_root],
    )
    assert normalize_repo(str(local_repo), settings) == str(local_repo.resolve())


def test_local_path_is_rejected_when_disabled(tmp_path: Path) -> None:
    local_root = tmp_path / "repos"
    local_repo = local_root / "my-service"
    local_repo.mkdir(parents=True)
    settings = make_settings(
        tmp_path,
        allow_local_repos=False,
        allowed_local_repo_roots=[local_root],
    )
    with pytest.raises(AppError) as excinfo:
        normalize_repo(str(local_repo), settings)
    assert excinfo.value.code == ErrorCode.LOCAL_REPO_NOT_ALLOWED


def test_missing_local_path_is_rejected(tmp_path: Path) -> None:
    local_root = tmp_path / "repos"
    missing_repo = local_root / "missing-service"
    settings = make_settings(
        tmp_path,
        allow_local_repos=True,
        allowed_local_repo_roots=[local_root],
    )
    with pytest.raises(AppError) as excinfo:
        normalize_repo(str(missing_repo), settings)
    assert excinfo.value.code == ErrorCode.LOCAL_REPO_NOT_FOUND


def test_file_local_path_is_rejected(tmp_path: Path) -> None:
    local_root = tmp_path / "repos"
    local_root.mkdir()
    file_path = local_root / "notes.txt"
    file_path.write_text("hello", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        allow_local_repos=True,
        allowed_local_repo_roots=[local_root],
    )
    with pytest.raises(AppError) as excinfo:
        normalize_repo(str(file_path), settings)
    assert excinfo.value.code == ErrorCode.LOCAL_REPO_NOT_DIRECTORY


def test_local_path_outside_allowed_root_is_rejected(tmp_path: Path) -> None:
    local_root = tmp_path / "repos"
    local_repo = tmp_path / "other" / "my-service"
    local_repo.mkdir(parents=True)
    settings = make_settings(
        tmp_path,
        allow_local_repos=True,
        allowed_local_repo_roots=[local_root],
    )
    with pytest.raises(AppError) as excinfo:
        normalize_repo(str(local_repo), settings)
    assert excinfo.value.code == ErrorCode.LOCAL_REPO_OUTSIDE_ALLOWED_ROOT


def test_url_with_embedded_password_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(AppError) as excinfo:
        normalize_repo("https://user:sekretpass@github.com/acme/repo", settings)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert "sekretpass" not in excinfo.value.user_message


def test_token_shaped_repo_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_repo("ghp_" + "A" * 36, make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_non_url_garbage_repo_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_repo("ftp://github.com/x", make_settings(tmp_path))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


# --- planning notes ---


def test_planning_notes_none_stays_none() -> None:
    assert normalize_planning_notes(None) is None


def test_planning_notes_blank_normalizes_to_none() -> None:
    assert normalize_planning_notes("   \n  ") is None


def test_planning_notes_trims_whitespace() -> None:
    assert normalize_planning_notes("  Use the existing retry helper.  ") == (
        "Use the existing retry helper."
    )


def test_planning_notes_token_shaped_is_rejected_without_echoing_value() -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_planning_notes(FAKE_TOKEN)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert FAKE_TOKEN not in excinfo.value.user_message


# --- requirement source (ticket xor uploaded document) ---


def test_ticket_only_is_accepted() -> None:
    require_exactly_one_requirement_source("PROJ-123", has_document=False)


def test_document_only_is_accepted() -> None:
    require_exactly_one_requirement_source(None, has_document=True)


def test_neither_ticket_nor_document_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        require_exactly_one_requirement_source(None, has_document=False)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_blank_ticket_and_no_document_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        require_exactly_one_requirement_source("   ", has_document=False)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_text_only_is_accepted() -> None:
    require_exactly_one_requirement_source(None, has_document=False, has_text=True)


@pytest.mark.parametrize(
    ("ticket", "has_document"), [("PROJ-123", False), (None, True), ("PROJ-123", True)]
)
def test_text_with_any_other_source_is_rejected(ticket: str | None, has_document: bool) -> None:
    with pytest.raises(AppError) as excinfo:
        require_exactly_one_requirement_source(ticket, has_document, has_text=True)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_mantis_with_any_other_source_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        require_exactly_one_requirement_source("PROJ-1", False, has_mantis=True)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    require_exactly_one_requirement_source(None, False, has_mantis=True)  # alone is fine


# --- Mantis issue numbers and links ---

MANTIS = "https://mantis.example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234),
        ("#1234", 1234),
        ("0001234", 1234),
        (" 1234 ", 1234),
        (f"{MANTIS}/view.php?id=1234", 1234),
        (f"{MANTIS}/view.php?id=0001234", 1234),
        (f"{MANTIS.upper()}/view.php?id=1234", 1234),
        (f"{MANTIS}/bug_view_page.php?bug_id=1234", 1234),
        (f"{MANTIS}/view.php?foo=1&id=1234", 1234),
    ],
)
def test_mantis_issue_accepts_numbers_and_links(raw: str, expected: int) -> None:
    assert normalize_mantis_issue(raw, MANTIS) == expected


def test_mantis_issue_blank_is_not_provided() -> None:
    assert normalize_mantis_issue(None, MANTIS) is None
    assert normalize_mantis_issue("  ", MANTIS) is None


@pytest.mark.parametrize(
    "raw",
    ["abc", "PROJ-12", "0", "12.5", f"{MANTIS}/view.php", f"{MANTIS}/view.php?id=abc"],
)
def test_mantis_issue_garbage_is_rejected(raw: str) -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_mantis_issue(raw, MANTIS)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_mantis_link_to_another_host_is_rejected() -> None:
    """The shared account's request must never be pointed somewhere else."""
    with pytest.raises(AppError) as excinfo:
        normalize_mantis_issue("https://mantis.evil.example/view.php?id=1234", MANTIS)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert "configured" in excinfo.value.user_message


def test_mantis_issue_token_shaped_is_rejected_without_echoing_it() -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_mantis_issue(FAKE_TOKEN, MANTIS)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert FAKE_TOKEN not in excinfo.value.user_message


# --- pasted requirement text ---


def test_requirement_text_blank_is_not_provided() -> None:
    assert normalize_requirement_text(None) is None
    assert normalize_requirement_text("  \n ") is None


def test_requirement_text_is_trimmed_but_keeps_its_lines() -> None:
    assert normalize_requirement_text("  First line\nSecond line \n") == "First line\nSecond line"


def test_requirement_text_over_the_limit_is_rejected_with_a_clear_message() -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_requirement_text("x" * 20_001)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert "20,000" in excinfo.value.user_message


def test_requirement_text_at_the_limit_is_accepted() -> None:
    assert normalize_requirement_text("x" * 20_000) == "x" * 20_000


def test_requirement_text_with_a_credential_is_rejected_without_echoing_it() -> None:
    with pytest.raises(AppError) as excinfo:
        normalize_requirement_text(f"Use this token to call the API: {FAKE_TOKEN}")
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert FAKE_TOKEN not in excinfo.value.user_message


def test_requirement_title_is_one_line_and_capped() -> None:
    assert normalize_requirement_title(None) is None
    assert normalize_requirement_title("  CSV\n  export  ") == "CSV export"
    title = normalize_requirement_title("t" * 500)
    assert title is not None
    assert len(title) == 200


def test_both_ticket_and_document_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        require_exactly_one_requirement_source("PROJ-123", has_document=True)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


# --- uploaded document validation ---


def test_valid_pdf_bytes_are_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    validate_uploaded_document(b"%PDF-1.4 minimal content", settings)


def test_non_pdf_bytes_are_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(AppError) as excinfo:
        validate_uploaded_document(b"not a pdf at all", settings)
    assert excinfo.value.code == ErrorCode.DOCUMENT_NOT_PDF


def test_empty_upload_is_rejected_as_not_pdf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(AppError) as excinfo:
        validate_uploaded_document(b"", settings)
    assert excinfo.value.code == ErrorCode.DOCUMENT_NOT_PDF


def test_oversized_upload_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, document_max_upload_bytes=10)
    with pytest.raises(AppError) as excinfo:
        validate_uploaded_document(b"%PDF-1.4 way more than ten bytes", settings)
    assert excinfo.value.code == ErrorCode.DOCUMENT_TOO_LARGE


# --- unknown form fields (invariant 1, multipart edition) ---


def test_known_fields_are_accepted() -> None:
    reject_unknown_form_fields({"ticket", "repo"}, JOB_CREATE_FORM_FIELDS)
    reject_unknown_form_fields(
        {"repo", "requirement_document", "document_ticket_key"}, JOB_CREATE_FORM_FIELDS
    )


def test_unknown_field_is_rejected_without_echoing_value() -> None:
    with pytest.raises(AppError) as excinfo:
        reject_unknown_form_fields({"ticket", "repo", "jira_token"}, JOB_CREATE_FORM_FIELDS)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert "jira_token" not in excinfo.value.user_message
