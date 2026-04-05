"""Structured JSON logging for Sunday Voice.

Emits one JSON object per log record on stdout so that systemd-journald (and any
downstream shipper) can index fields individually. Includes correlation via the
request_id contextvar populated by ``RequestIDMiddleware``.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        rid = request_id_ctx.get()
        if rid:
            payload["request_id"] = rid

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "info") -> None:
    """Install the JSON formatter on the root logger.

    Safe to call multiple times; existing handlers are replaced.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Align common noisy loggers with the app level; keep access logging off
    # since we emit our own structured request log via middleware.
    for name in ("uvicorn", "uvicorn.error", "fastapi"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
        logger.setLevel(level.upper())

    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
