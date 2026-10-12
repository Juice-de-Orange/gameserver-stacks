"""Result texts that carry a value travel as `{code, message, params}` (`app/errors.note`).

A plain English sentence with a value built in ("Backup x.tar.gz restored", "… failed (db gone)")
cannot be translated by the frontend's catalog of fixed backend texts (`tb`), so the German UI
showed it in English. Coded, the frontend translates by code and fills in the values.
"""
import ast
import json
from pathlib import Path

from app.services import operations

APP = Path(__file__).resolve().parents[1] / "app"

# Where the operation panel's success line (`summary`) and its note come from, and the
# whitelist note of the moderation page.
SOURCES = [
    "services/operations.py",
    "services/restore.py",
    "services/version_manager.py",
    "services/mods/apply.py",
    "services/mods/avorion_apply.py",
    "routers/server_ops.py",
    "services/drivers/avorion.py",
]


def _f_strings_in_result_texts(path: Path) -> list[int]:
    """Lines where a `"summary"`/`"note"` value, or a variable called `note`, is built with an f-string."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            values += [v for k, v in zip(node.keys, node.values, strict=True)
                       if isinstance(k, ast.Constant) and k.value in ("summary", "note")]
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "note" for t in node.targets):
                values.append(node.value)
    return sorted({n.lineno for v in values for n in ast.walk(v) if isinstance(n, ast.JoinedStr)})


def test_no_result_text_has_a_value_built_in():
    found = {rel: lines for rel in SOURCES if (lines := _f_strings_in_result_texts(APP / rel))}
    assert not found, f"f-string result texts (use errors.note with params): {found}"


def test_the_guard_would_find_one(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('def f(n):\n    return {"summary": f"Backup {n} restored"}\n', encoding="utf-8")
    assert _f_strings_in_result_texts(bad) == [2]


def test_a_truncated_result_says_so_with_a_code():
    text = operations._result_json({"summary": "done", "files": ["x" * 50] * 200})
    brief = json.loads(text)
    assert brief["truncated"] is True
    assert brief["note"]["code"] == "operation.result_truncated"
    assert brief["note"]["params"]["length"] > operations.RESULT_MAX
    assert brief["summary"] == "done"


def test_a_coded_summary_becomes_the_english_detail():
    """`detail` stays a plain sentence (logs, curl); the code travels in `result.summary`."""
    from app.errors import note

    summary = note("restore.done", "Backup {name} restored", name="b.tar.gz")
    assert operations._final_text({"summary": summary}, "backup.restore") == "Backup b.tar.gz restored"
    assert operations._final_text({"summary": "plain"}, "backup.restore") == "plain"
    assert operations._final_text({}, "backup.restore") == "The backup was restored."
