"""Input validation — security invariants 1 and 5 (backend half)."""

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.schemas.inputs import (
    JOB_CREATE_FORM_FIELDS,
    normalize_planning_notes,
    normalize_repo,
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


def test_unknown_field_is_rejected_without_echoing_value() -> None:
    with pytest.raises(AppError) as excinfo:
        reject_unknown_form_fields({"ticket", "repo", "jira_token"}, JOB_CREATE_FORM_FIELDS)
    assert excinfo.value.code == ErrorCode.INPUT_INVALID
    assert "jira_token" not in excinfo.value.user_message
