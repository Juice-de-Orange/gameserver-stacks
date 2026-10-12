"""Texts the API sends to a user: a stable code, English text, and the values that went into it.

The frontend translates by `code` and fills in `params`; `message` is the finished English
sentence for everything that does not translate (curl, logs, a frontend older than the backend).

    raise HTTPException(404, detail=err("server.not_found", "Server '{key}' not found", key=key))

Code and message are literals at the call site, on purpose: `tests/test_error_codes.py` reads
them from the source and compares them with the frontend catalogs
(`frontend/src/i18n/locales/<language>/api-*.json`), so a new code without a German text, or an
English text that drifted from the catalog, fails the standard run.
"""
from __future__ import annotations

from typing import Any


def err(code: str, message: str, /, **params: Any) -> dict[str, Any]:
    """An error detail: `{"code", "message", "params"}`. Params must be JSON-serialisable."""
    return {"code": code, "message": message.format(**params) if params else message, "params": params}


def note(code: str, message: str, /, **params: Any) -> dict[str, Any]:
    """The same shape for a text that is not an error (a finding, a hint, a progress step)."""
    return err(code, message, **params)


def detail_of(exc: BaseException) -> dict[str, Any] | str:
    """What to send for an exception a service raised: its coded `detail` if it carries one
    (`drivers.DriverError`), otherwise its text."""
    detail = getattr(exc, "detail", None)
    return detail if isinstance(detail, dict) else str(exc)
