"""Structured JSON logging with secret redaction.

Every log line is one JSON object. Secrets are removed by a filter that runs on
every record, so a careless `log.info(url)` cannot leak an API key.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SECRET_KEYS = re.compile(r"(api[_-]?key|apikey|password|secret|token|authorization)", re.I)
_SECRET_IN_TEXT = [
    re.compile(r"(apiKey=)[^&\s\"']+", re.I),
    re.compile(r"(api_key=)[^&\s\"']+", re.I),
    re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
]
REDACTED = "***REDACTED***"


def redact_text(text: str) -> str:
    for pattern in _SECRET_IN_TEXT:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", text)
        else:
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return text


def redact_value(key: str, value: Any) -> Any:
    if _SECRET_KEYS.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact_value(k, v) for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    # Logs go to stderr so command output on stdout stays machine-readable.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs (which contain the API key) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
