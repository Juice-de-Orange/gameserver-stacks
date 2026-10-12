"""Targeted read/write of INDIVIDUAL SandboxVars in `<servername>_SandboxVars.lua`.

Deliberately conservative: only scalar `Key = value,` lines (top level plus one nesting level,
e.g. ZombieLore.Speed). Replaces only the value part of an exactly matched line and leaves
everything else untouched — unknown or anomalous lines raise an error instead of corrupting the
file. The raw editor remains the full escape hatch.
"""
from __future__ import annotations

import re

from ..services import ini_editor as ini

_OPEN = re.compile(r"^([A-Za-z_]\w*)\s*=\s*\{")
_KV = re.compile(r"^([A-Za-z_]\w*)\s*=\s*([^,{}]+?)\s*,?\s*$")
# Value replacement that preserves prefix and suffix (comma/comment):
_LINE = re.compile(r"^(\s*[A-Za-z_]\w*\s*=\s*)([^,{}]+?)(\s*,.*|\s*)$")


def _parse(text: str) -> dict[str, tuple[str, int]]:
    """{path: (value, line index)} — path without the leading 'SandboxVars.'."""
    result: dict[str, tuple[str, int]] = {}
    stack: list[str] = []
    for i, line in enumerate(text.splitlines()):
        s = line.split("--", 1)[0].strip()  # strip Lua inline comment
        if not s:
            continue
        m_open = _OPEN.match(s)
        if m_open:
            stack.append(m_open.group(1))
            continue
        if s.startswith("}"):
            if stack:
                stack.pop()
            continue
        m_kv = _KV.match(s)
        if m_kv:
            path = ".".join(stack + [m_kv.group(1)]) if stack else m_kv.group(1)
            if path.startswith("SandboxVars."):
                path = path[len("SandboxVars."):]
            result[path] = (m_kv.group(2), i)
    return result


def read_values(server) -> dict[str, str]:
    """Read the server's SandboxVars."""
    try:
        text = ini.read_raw(ini.sandbox_path(server))
    except OSError:
        return {}
    return {path: val for path, (val, _idx) in _parse(text).items()}


def write_values(updates: dict[str, str], server) -> None:
    text = ini.read_raw(ini.sandbox_path(server))
    lines = text.splitlines()
    parsed = _parse(text)
    for path, newval in updates.items():
        if path not in parsed:
            raise ValueError(f"Unknown SandboxVar: {path}")
        idx = parsed[path][1]
        m = _LINE.match(lines[idx])
        if not m:
            raise ValueError(f"Line cannot be edited: {path}")
        lines[idx] = f"{m.group(1)}{newval}{m.group(3) or ''}"
    ini.write_raw(ini.sandbox_path(server), "\n".join(lines) + "\n")
