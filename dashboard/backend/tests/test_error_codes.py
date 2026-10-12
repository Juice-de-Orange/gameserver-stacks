"""Every coded text of the API has an English and a German entry in the frontend catalogs.

`app/errors.py` gives each user-facing text a code; the frontend translates by that code. This
test reads the calls from the source (code and message are literals there) and the catalogs from
`frontend/src/i18n/locales/<language>/api-*.json`, so the two cannot drift apart unnoticed.
"""
import ast
import json
import re
from pathlib import Path

from app.errors import err, note

BACKEND = Path(__file__).resolve().parents[1]
LOCALES = BACKEND.parent / "frontend" / "src" / "i18n" / "locales"
HELPERS = {"err", "note"}
CODE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _calls() -> list[tuple[str, int, ast.Call]]:
    found = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        if path.name == "errors.py":
            continue  # `note` forwards to `err` there; that call carries no literals
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in HELPERS:
                found.append((str(path.relative_to(BACKEND)), node.lineno, node))
    return found


def _catalog(language: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted((LOCALES / language).glob("api-*.json")):
        for key, text in json.loads(path.read_text(encoding="utf-8")).items():
            assert key not in merged, f"{key} is defined twice in the {language} api catalogs"
            merged[key] = text
    return merged


def _used() -> dict[str, str]:
    """code -> English message, as written at the call sites."""
    used: dict[str, str] = {}
    for file, line, call in _calls():
        where = f"{file}:{line}"
        assert len(call.args) == 2, f"{where}: code and message are the two positional arguments"
        code, message = call.args
        assert isinstance(code, ast.Constant) and isinstance(code.value, str), f"{where}: the code must be a literal"
        assert isinstance(message, ast.Constant) and isinstance(message.value, str), (
            f"{where}: the message must be a plain string literal (no f-string) — pass values as params"
        )
        assert CODE.match(code.value), f"{where}: '{code.value}' is not a dotted lower-case code"
        params = {kw.arg for kw in call.keywords}
        assert None not in params, f"{where}: pass params by name, not as **dict"
        placeholders = set(PLACEHOLDER.findall(message.value))
        assert placeholders == params, f"{where}: placeholders {placeholders} and params {params} differ"
        assert not re.search(r"[{}]", PLACEHOLDER.sub("", message.value)), f"{where}: stray brace in the message"
        assert used.setdefault(code.value, message.value) == message.value, (
            f"{where}: '{code.value}' is used with two different messages"
        )
    return used


def test_the_helpers_return_code_message_and_params():
    assert err("server.not_found", "Server '{key}' not found", key="pz-1") == {
        "code": "server.not_found",
        "message": "Server 'pz-1' not found",
        "params": {"key": "pz-1"},
    }
    assert note("a.b", "plain") == {"code": "a.b", "message": "plain", "params": {}}


def test_every_code_is_in_the_english_catalog_with_the_same_text():
    english = _catalog("en")
    for code, message in _used().items():
        assert english.get(code) == PLACEHOLDER.sub(r"{{\1}}", message), f"en catalog differs for '{code}'"


def test_every_code_has_a_german_text_with_the_same_placeholders():
    german = _catalog("de")
    for code, message in _used().items():
        assert code in german, f"no German text for '{code}'"
        assert set(re.findall(r"\{\{([a-z_][a-z0-9_]*)\}\}", german[code])) == set(PLACEHOLDER.findall(message)), (
            f"placeholders differ in the German text for '{code}'"
        )


def test_the_catalogs_hold_no_code_the_backend_does_not_use():
    used = set(_used())
    assert used, "no coded text found — the scan is broken"
    for language in ("en", "de"):
        assert set(_catalog(language)) - used == set(), f"unused codes in the {language} api catalogs"


def test_every_plain_backend_text_in_the_catalogs_exists_in_the_source():
    """`backend-*.json` is keyed by the English text the backend sends; a key nobody sends is dead."""
    literals: set[str] = set()
    for path in sorted((BACKEND / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
    for language in ("en", "de"):
        for path in sorted((LOCALES / language).glob("backend-*.json")):
            for key in json.loads(path.read_text(encoding="utf-8")):
                assert key in literals, f"{path.name} ({language}): no such text in the backend: {key!r}"
