"""Guard: in the world-writing core nobody asks for `running` directly.

**What it stands against.** `docker.state()` returns `running` **and** `restarting`. The question
before every world write is "is this container in someone else's hands", and that is
`running or restarting`, not `running` alone.

**The original failure assumption is refuted**, and that belongs here: the claim was that Docker
reports `running=false` + `restarting=true` in a restart backoff. Measured on real Docker (300
samples across a crash loop): `Running` is **never** `false` — moby sets both to `true` during a
restart. The described damage cannot happen here. Details in `lifecycle.running_or_restarting`.

**Why the guard exists anyway.** It does not hold shut a fixed bug but keeps a QUESTION in one
place: the same hand copy stood in three places (`_is_running`, `stop_and_confirm`,
`mods/apply.restore_quarantine`) and a fourth turned up only while refuting
(`test_instance.clone_to_test`, with two `rmtree` behind it). Three copies of one question are a
problem regardless of Docker semantics — the next copy picks the sign the other way round. That
is exactly how a four-times hand-copied stop confirmation came about, one copy of which was
inverted.

**The rule.** In the modules that touch world files, `.get("running")` is not read directly. The
question goes through `lifecycle.running_or_restarting` or `lifecycle.is_running`.

**The exceptions are measured, not estimated** — there are two, both with a reason. If the list
were longer than the rule, this guard would not exist, only the measurement.

Outside these modules `.get("running")` stays allowed: display, metric samples, log parsers and
the idle stop answer no world-write question with it (`stats_poller`, `metrics`, `watchdog`,
`idle_autostop`, `log_parser`, `broadcasts`, `cross_server/*`, `resource_budget`, `routers/*`,
`cli.py`).
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# The modules in which the question is a WORLD-WRITE decision.
WORLD_CORE = (
    "services/lifecycle.py",
    "services/backup.py",
    "services/restore.py",
    "services/version_manager.py",
)

ALLOWED = {
    # The choke point itself — it IS the evaluation of both fields.
    ("services/lifecycle.py", "running_or_restarting"),
    # The boot proof deliberately evaluates `running`, `restarting` and the restart counter
    # separately: it does not ask "is it in someone else's hands" but "is it running STABLY".
    ("services/version_manager.py", "verify_boot"),
}


def _in_world_core(rel: str) -> bool:
    return rel in WORLD_CORE or rel.startswith("services/mods/")


def _reads_on_a_state(receiver: ast.expr) -> bool:
    """Is this a read directly on the result of a `...state(...)` call?"""
    for node in ast.walk(receiver):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "state":
                return True
            if isinstance(f, ast.Name) and f.id == "state":
                return True
    return False


def _running_reads(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, enclosing function) for every `<anything>.get("running")` in the file.

    The form is captured, not the receiver: `st.get("running")`,
    `(await docker.state(n)).get("running")` and `state.get("running")` are the same question.

    **A variable key is reported only when read directly on a `state(...)` result.** Reporting
    every `anything.get(variable)` as unclear would hit `_locks.get(key)`, `plan.get(category)`
    and a dozen places that have nothing to do with the question. A guard whose exception list
    would be longer than its rule is not built in this project — so the rule is narrow instead of
    the list long.

    What would slip through: a key that reaches an ordinary dict through an intermediate
    variable. That form does not occur anywhere in `app/`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # line -> name of the enclosing function (the innermost wins).
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                enclosing[line] = node.name

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get" and node.args):
            continue
        first = node.args[0]
        where = enclosing.get(node.lineno, "<module>")
        if isinstance(first, ast.Constant):
            if first.value == "running":
                hits.append((node.lineno, where))
        elif isinstance(first, ast.Name) and _reads_on_a_state(func.value):
            hits.append((node.lineno, where + " [unclear]"))

    # **The index form `st["running"]` counts too.** It is no invention — `routers/resources.py`
    # already writes `live["running"] = ...`. A guard that only catches the spelling that last
    # occurred catches the form, not the class.
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "running":
                hits.append((node.lineno, enclosing.get(node.lineno, "<module>")))
    return sorted(set(hits))


def test_the_world_core_does_not_read_running_itself():
    violations: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if not _in_world_core(rel):
            continue
        for line, function in _running_reads(path):
            if (rel, function) in ALLOWED:
                continue
            violations.append(f"{rel}:{line} in {function}()")
    assert not violations, (
        "These places decide about a world write and overlook the restart backoff "
        "(`restarting=true`). They belong behind "
        "`lifecycle.running_or_restarting` or `lifecycle.is_running`:\n  "
        + "\n  ".join(violations)
    )


def test_the_guard_would_find_the_old_bug(tmp_path):
    """Cross-check — the code as it once stood in `stop_and_confirm`."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def stop_and_confirm(server):\n"
        '    still_running = bool((await docker.state(name)).get("running"))\n',
        encoding="utf-8",
    )
    assert _running_reads(bad) == [(2, "stop_and_confirm")]


def test_the_guard_does_not_fall_for_workarounds(tmp_path):
    """Another receiver and a variable key on a `state(...)` — both are caught."""
    bad = tmp_path / "tricky.py"
    bad.write_text(
        "async def f():\n"
        '    a = st.get("running")\n'
        "    b = (await docker.state(n)).get(field)\n"
        "    c = _locks.get(key)\n",
        encoding="utf-8",
    )
    assert _running_reads(bad) == [(2, "f"), (3, "f [unclear]")]


def test_other_fields_stay_untouched(tmp_path):
    """The guard may only hit this one question."""
    ok = tmp_path / "good.py"
    ok.write_text(
        "def f():\n"
        '    x = st.get("restarts")\n'
        '    y = st.get("health")\n'
        "    z = lifecycle.running_or_restarting(st)\n",
        encoding="utf-8",
    )
    assert _running_reads(ok) == []


def test_the_exception_list_is_shorter_than_the_rule():
    """Project rule: a guard with a long exception list is not built; the places are measured.

    Two exceptions, both with a reason in the source. If the list grows, the guard is the wrong
    tool — then it should be replaced, not extended.
    """
    assert len(ALLOWED) <= 3, (
        f"{len(ALLOWED)} exceptions — from here on, measure the places and write the measurement "
        "down instead of maintaining a guard that is more exception than rule."
    )
