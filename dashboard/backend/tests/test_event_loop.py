"""No blocking call inside a coroutine.

The backend runs with **exactly one** uvicorn worker (a hard constraint).
Whatever blocks inside an `async def` therefore does not freeze "one request" but the whole
application: every HTTP response, every WebSocket, every scheduler tick. Two places did that:

* `steam_download.download_item` mirrored the mod folder with `shutil.copytree` **synchronously** —
  PZ workshop maps are hundreds of MB to several GB. That is seconds to minutes of complete
  standstill in which the dashboard looks like a backend outage.
* `bercon.bercon_command` resolved the container name with `socket.gethostbyname` — a blocking
  libc call **without** a timeout parameter, triggered by the stats poller every minute.
  Invisible normally; when Docker's embedded resolver does not answer (dockerd restart, container
  recreate — exactly the situation the re-resolving was introduced for), `resolv.conf`'s
  `timeout:5, attempts:2` apply and the whole API stands still for up to ten seconds.

The remedies in this code base are `asyncio.to_thread` and `loop.getaddrinfo`.

**Limit, stated honestly:** the scan is one level deep and purely syntactic. It does not see that
a *synchronous helper* called from a coroutine blocks in turn — that would need a call graph, and
that would cost more than the bug. Nor does it see a CPU-bound loop or a blocking library
(`berconpy`, a synchronous `httpx`). What it does see, it sees completely and without judgement.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).parent.parent / "app"

# Two tiers, because the names differ in how much they tell.
#
# UNAMBIGUOUS: the name alone is the signal. The search deliberately goes by the attribute name
# (like `test_stop_paths.py`), so that `from shutil import copytree` and any alias are caught
# as well.
UNAMBIGUOUS = {
    "copytree", "rmtree", "copy2", "copyfile", "move", "make_archive", "unpack_archive",
    "gethostbyname", "gethostbyaddr", "check_output", "check_call",
    # tarfile runs over a game world: minutes for a ~4 GB Minecraft world. Since
    # `POST /{key}/backups` is a background operation instead of a synchronous endpoint (thread
    # pool, harmless), the same call runs IN the event loop when someone writes it without
    # `to_thread` — an easy mistake that nobody notices, because the result is correct.
    "create_backup", "extract_backup", "backup_contents", "missing_parts", "check_restorable",
}

# GENERIC: the same name exists in the project in harmless forms and therefore only counts with
# the right module in front of it. Without this split the guard reported `docker.rename` (an
# `async def` on the Docker client) and a locally defined `run()` in `create_task(run())` — both
# false alarms that would have bloated an exception list instead of sharpening the rule.
GENERIC = {
    ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "Popen"),
    ("time", "sleep"),
    ("socket", "create_connection"),
    ("os", "walk"), ("os", "replace"), ("os", "makedirs"), ("os", "removedirs"),
    ("os", "rename"), ("os", "remove"), ("os", "listdir"), ("os", "rmdir"),
    ("shutil", "disk_usage"),
}

# Deliberately allowed. The key is `file:call`, NOT the line number: otherwise the guard breaks
# with every line inserted above the hit and points the developer at code they did not touch.
# Every entry has a reason and an expected count — an exception list without reasons eventually
# becomes a permission nobody remembers.
ALLOWED: dict[str, tuple[int, str]] = {
    "app/services/mods/apply.py:open": (
        1,
        "`open()` on the quarantine manifest, reading in `restore_quarantine`: ~1 KB, local "
        "directory, no network. A to_thread for it costs more context switches than it saves "
        "blocking. **The WRITE lives in `_write_manifest`** — a synchronous function called "
        "from the coroutine, which this guard therefore no longer sees (it reads per function, "
        "not across calls). It still blocks, and for the same reason that is still fine; it is "
        "noted here so the lower count does not look like progress that it is not.",
    ),
}


class _Finder(ast.NodeVisitor):
    """Reports blocking calls, but only inside an `async def`.

    Nesting counts correctly: a **synchronous** function defined *inside* a coroutine (the usual
    `def _work(): …` before a `to_thread`) is not loop context.
    """

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved, self._depth = self._depth, 0   # synchronous function → no loop context
        self.generic_visit(node)
        self._depth = saved

    def visit_Call(self, node: ast.Call) -> None:
        if self._depth > 0:
            name, module = None, None
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
                if isinstance(node.func.value, ast.Name):
                    module = node.func.value.id
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            full = f"{module + '.' if module else ''}{name}"
            if name == "open" and module is None:
                self.hits.append((node.lineno, "open"))
            elif name in UNAMBIGUOUS:
                self.hits.append((node.lineno, full))
            elif (module, name) in GENERIC:
                self.hits.append((node.lineno, full))
        self.generic_visit(node)


def _blocking_calls(tree: ast.AST) -> list[tuple[int, str]]:
    s = _Finder()
    s.visit(tree)
    return sorted(s.hits)


def test_no_blocking_call_inside_a_coroutine():
    violations: list[str] = []
    counted: dict[str, int] = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        for lineno, what in _blocking_calls(ast.parse(path.read_text(encoding="utf-8"))):
            key = f"{rel}:{what}"
            if key in ALLOWED:
                counted[key] = counted.get(key, 0) + 1
                continue
            violations.append(f"{rel}:{lineno} → {what}()")
    assert not violations, (
        "These calls stall the ONE uvicorn worker — meanwhile the whole API does not answer,\n"
        "no WebSocket, no scheduler tick. Use `asyncio.to_thread(...)` or\n"
        "`loop.getaddrinfo(...)`:\n  " + "\n  ".join(violations)
    )
    # Against decay: an exception that no longer matches anything is a permission nobody checks —
    # and one that matches MORE than agreed silently covers new hits as well.
    for key, (expected, _reason) in ALLOWED.items():
        actual = counted.get(key, 0)
        assert actual == expected, (
            f"exception {key}: {actual} hits instead of the agreed {expected}. "
            "Either one went away (delete the entry) or a new one appeared "
            "(which is NOT covered by this reason)."
        )


def test_the_guard_would_find_the_old_bugs(tmp_path):
    """Cross-check — and the evidence that it knows the four obvious evasions."""
    p = tmp_path / "old.py"
    p.write_text(
        "async def download_item(wid, server):\n"
        "    shutil.copytree(src, staging)\n"          # 2  the copytree bug
        "    os.replace(staging, dst)\n"               # 3
        "    await asyncio.sleep(1)\n"                 # 4  NOT meant
        "\n"
        "async def bercon_command(host):\n"
        "    host = socket.gethostbyname(host)\n"      # 7  the gethostbyname bug
        "    await asyncio.to_thread(_work)\n"
        "\n"
        "async def clean(x):\n"
        "    def _work():\n"
        "        shutil.copytree(a, b)\n"              # 12 in a SYNCHRONOUS function → ok
        "    return await asyncio.to_thread(_work)\n"
        "\n"
        "def synchronous_is_allowed():\n"
        "    shutil.rmtree(x)\n"                       # 16 not a coroutine at all → ok
        "\n"
        "async def backup_endpoint(server):\n"
        "    return bk.create_backup('manual')\n",     # 19 since backups became an operation
        encoding="utf-8",
    )
    found = _blocking_calls(ast.parse(p.read_text(encoding="utf-8")))
    assert found == [
        (2, "shutil.copytree"),
        (3, "os.replace"),
        (7, "socket.gethostbyname"),
        (19, "bk.create_backup"),
    ], f"unexpected: {found}"
