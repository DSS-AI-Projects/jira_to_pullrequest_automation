"""Redacting logging setup — security invariant 2.

All log records pass through RedactionFilter, which masks (a) every secret
value registered via core.secrets and (b) token-shaped strings by pattern,
including inside formatted exception tracebacks.
"""

from __future__ import annotations

import logging
import traceback

from app.core import secrets

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Mask registered secret values and token-shaped substrings."""
    for value in secrets.registered_secrets():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in secrets.TOKEN_PATTERNS:
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
