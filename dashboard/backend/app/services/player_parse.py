"""Parser for the PZ `players` RCON output — the single source for router, driver and poller.

The former call sites import this function under their old local names.
"""
from __future__ import annotations

import re

_COUNT_RE = re.compile(r"\((\d+)\)")


def parse_players(out: str) -> dict:
    """RCON `players` output → {"count": int, "names": list[str]}.

    Example: 'Players connected (2):\\n-Alice\\n-Bob' → {"count": 2, "names": ["Alice", "Bob"]}.
    """
    match = _COUNT_RE.search(out)
    count = int(match.group(1)) if match else 0
    names = [line.lstrip("-").strip() for line in out.splitlines() if line.strip().startswith("-")]
    return {"count": count, "names": names}
