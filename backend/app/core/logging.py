"""Redacting logging setup — security invariant 2.

All log records pass through RedactionFilter, which masks (a) every secret
value registered via core.secrets and (b) token-shaped strings by pattern,
including inside formatted exception tracebacks.
"""

from __future__ import annotations

import logging
import re
import traceback

from app.core import secrets

REDACTED = "[REDACTED]"

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Atlassian API token
    re.compile(r"ATATT[0-9A-Za-z_\-=+/]{10,}"),
    # Anthropic API key
    re.compile(r"sk-ant-[0-9A-Za-z_\-]{10,}"),
    # GitHub tokens (classic + fine-grained)
    re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    # GitLab / Bitbucket tokens
    re.compile(r"glpat-[0-9A-Za-z_\-]{15,}"),
    re.compile(r"BBDC-[0-9A-Za-z_\-]{10,}"),
    # HTTP auth headers
    re.compile(r"(?i)\b(?:basic|bearer)\s+[0-9A-Za-z+/_\-.=]{16,}"),
    # userinfo credentials embedded in URLs (https://user:pass@host)
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    # private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
    """Mask registered secret values and token-shaped substrings."""
    for value in secrets.registered_secrets():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if record.exc_info and record.exc_info != (None, None, None):
            exc_type, exc, tb = record.exc_info
            formatted = "".join(traceback.format_exception(exc_type, exc, tb))
            message = f"{message}\n{formatted}"
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = ()
        return True


_FILTER = RedactionFilter()


def configure_logging(level: int = logging.INFO) -> None:
    """Install a redacting handler on the root logger and on uvicorn's loggers."""
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(f, RedactionFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(_FILTER)
        root.addHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
