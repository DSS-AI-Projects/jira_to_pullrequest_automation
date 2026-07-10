"""Security invariant 2: secrets never appear in logs.

Fake tokens are constructed at runtime (concatenation) so this source file
itself never contains a token-shaped literal that would trip the secret scanner.
"""

import io
import logging

import pytest

from app.core import secrets
from app.core.logging import REDACTED, RedactionFilter, redact

FAKE_ATLASSIAN = "ATATT" + "3xFfGF0" + "a" * 24
FAKE_ANTHROPIC = "sk-ant-" + "api03-" + "b" * 32
FAKE_GITHUB = "ghp_" + "C" * 36
FAKE_BASIC_HEADER = "Basic " + "dXNlcjpwYXNzd29yZA" + "=" * 2


def make_capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_token_shaped_patterns_are_redacted() -> None:
    for token in (FAKE_ATLASSIAN, FAKE_ANTHROPIC, FAKE_GITHUB, FAKE_BASIC_HEADER):
        out = redact(f"calling api with {token} now")
        assert token not in out
        assert REDACTED in out


def test_url_userinfo_credentials_are_redacted() -> None:
    out = redact("cloning https://user:hunter2secret@github.com/org/repo.git")
    assert "hunter2secret" not in out


def test_registered_secret_value_is_redacted_even_if_not_token_shaped() -> None:
    value = "correct horse battery staple"
    secrets.register_secret(value)
    out = redact(f"token is {value}")
    assert value not in out
    assert REDACTED in out


def test_short_values_are_not_registered() -> None:
    secrets.register_secret("short")
    assert "short" not in secrets.registered_secrets()


def test_logger_redacts_message_and_args() -> None:
    logger, stream = make_capturing_logger("test.redact.args")
    logger.info("auth header: %s", FAKE_ANTHROPIC)
    assert FAKE_ANTHROPIC not in stream.getvalue()
    assert REDACTED in stream.getvalue()


def test_logger_redacts_exception_traceback() -> None:
    logger, stream = make_capturing_logger("test.redact.exc")
    try:
        raise RuntimeError(f"401 calling jira with {FAKE_ATLASSIAN}")
    except RuntimeError:
        logger.exception("jira call failed")
    output = stream.getvalue()
    assert FAKE_ATLASSIAN not in output
    assert "RuntimeError" in output  # traceback still present, just redacted


def test_secrets_read_through_module_are_auto_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = "ATATT" + "z" * 30
    monkeypatch.setenv("JIRA_API_TOKEN", fake)
    assert secrets.get_jira_api_token() == fake
    assert fake in secrets.registered_secrets()
    assert redact(f"x {fake} y") == f"x {REDACTED} y"
