"""Keep access tokens out of the uvicorn logs.

WebSockets cannot set an `Authorization` header, so the frontend appends the JWT access token as
a query parameter to every `/ws/*` URL (`frontend/src/lib/ws.ts`). uvicorn logs the **full**
query string, which would put a still-valid token in clear text into the container log — usable
by anyone who may read that log.

Two loggers are affected, not one:
- `uvicorn.access` — the regular request line.
- `uvicorn.error` — this is where the WebSocket line `"WebSocket /ws/logs?..." [accepted]` ends up.

The filter is attached to the logger, not the handler: handler filters are replaced when
`logging.config` is reloaded, logger filters survive that.
"""
from __future__ import annotations

import logging
import re

# `token=` up to the next delimiter (&, whitespace, quote, end of line).
# `access_token=`/`refresh_token=` are covered as well — the other names under which the same
# secret could show up in the future. `ticket=` is the one-time WebSocket ticket.
_TOKEN_RE = re.compile(r"((?:access_|refresh_)?token=|ticket=)[^&\s\"']+")

_MASK = r"\1***"


class MaskTokensFilter(logging.Filter):
    """Replaces token values in log lines with `***`. Always returns True (drops nothing)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                _TOKEN_RE.sub(_MASK, a) if isinstance(a, str) else a for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(_MASK, record.msg)
        return True


def install() -> None:
    """Attach the filter to the uvicorn loggers. Idempotent — the lifespan may run more than once."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, MaskTokensFilter) for f in logger.filters):
            logger.addFilter(MaskTokensFilter())
