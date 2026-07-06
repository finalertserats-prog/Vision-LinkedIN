"""Structured JSON logging with run-id correlation and secret redaction.

WHY this module exists: BRD §17/§22 require structured logs correlated by
``run_id`` for observability, and NFR-05/§22 require that secrets NEVER appear in
logs. This module provides both in one place: a JSON formatter, a contextvar
that threads a ``run_id`` through every log record, and a filter that masks any
token/secret/key-like value before it is ever written.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# --- Run-id correlation ----------------------------------------------------
# A contextvar lets every stage of a daily run stamp its logs with the same
# run_id without passing it through every function signature. Defaults to "-"
# so records emitted outside a run still format cleanly.
_run_id_var: ContextVar[str] = ContextVar("vision_run_id", default="-")


def set_run_id(run_id: str) -> None:
    """Bind ``run_id`` to the current execution context.

    Called at the start of a daily run so all subsequent log records — across
    ingest/curate/synthesise/email — are correlated to that run (§17).
    """
    _run_id_var.set(run_id)


def get_run_id() -> str:
    """Return the run_id bound to the current context (or ``"-"`` if unset)."""
    return _run_id_var.get()


# --- Secret redaction -------------------------------------------------------
# Two-pronged masking so secrets never leak (NFR-05):
#   1. Key-name match: any log 'extra' whose key looks sensitive is masked.
#   2. Value-pattern match: long token-like blobs and bearer tokens in free text
#      are masked even when the author didn't flag them.

# Substrings that mark a field name as sensitive.
_SENSITIVE_KEY_HINTS = ("token", "secret", "key", "password", "authorization", "cookie")

# Free-text patterns that look like credentials regardless of the field name.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),  # HTTP bearer tokens
    re.compile(r"\b[A-Za-z0-9._\-]{32,}\b"),  # long opaque token-like blobs
)

_MASK = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    """Return True if a field name hints at a secret (case-insensitive)."""
    lowered = key.lower()
    return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)


def _redact_value(value: Any) -> Any:
    """Recursively mask secrets in a value.

    Dicts/lists are walked so nested structures (e.g. a serialized request) are
    scrubbed; strings are pattern-masked; other scalars pass through untouched.
    """
    if isinstance(value, dict):
        # Mask by key name first, then recurse into surviving values.
        return {
            k: (_MASK if _is_sensitive_key(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _VALUE_PATTERNS:
            redacted = pattern.sub(_MASK, redacted)
        return redacted
    return value


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs secrets from every record before formatting.

    Runs as a filter (not just in the formatter) so the masking also applies to
    any handler and to the record's ``extra`` fields, closing the leak surface.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Mask the rendered message text (covers f-strings / %-args already merged).
        if isinstance(record.msg, str):
            record.msg = _redact_value(record.msg)
        # Mask structured extras attached via ``logger.info(..., extra={...})``.
        for attr, val in list(record.__dict__.items()):
            if attr in _RESERVED_RECORD_ATTRS:
                continue
            if _is_sensitive_key(attr):
                record.__dict__[attr] = _MASK
            else:
                record.__dict__[attr] = _redact_value(val)
        return True  # never drop records — redaction only mutates them


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with run-id correlation."""

    def format(self, record: logging.LogRecord) -> str:
        # Base envelope: timestamp is UTC ISO-8601 for machine correlation.
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": get_run_id(),
            "msg": record.getMessage(),
        }
        # Attach any non-reserved extras (already redacted by the filter).
        for attr, val in record.__dict__.items():
            if attr not in _RESERVED_RECORD_ATTRS and attr not in payload:
                payload[attr] = val
        # Include exception info when present so stack traces are captured
        # structurally rather than lost.
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


# Standard LogRecord attributes we must not treat as user 'extra' fields.
_RESERVED_RECORD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime"}


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter + redaction filter on the root logger.

    Idempotent: replaces existing handlers so repeated calls (e.g. per cron
    invocation or per test) don't stack duplicate handlers.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()  # avoid duplicate log lines across re-configuration
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Thin wrapper kept for a stable import surface."""
    return logging.getLogger(name)
