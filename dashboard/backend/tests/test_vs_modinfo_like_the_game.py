"""Read `modinfo.json` the way Vintage Story itself does (lenient JSON, as found on a real server)."""
import io
import zipfile

from app.services.mods.foreign import _vs_json, read_metadata


def _zip(tmp_path, name: str, modinfo: str):
    path = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modinfo.json", modinfo)
    path.write_bytes(buf.getvalue())
    return str(path)


def test_comma_before_bracket_is_not_a_broken_archive(tmp_path):
    # Verbatim from genelib_3.2.1.zip — the dashboard used to block it as MODFILE_UNREADABLE.
    info = '{ "modid": "genelib", "version": "3.2.1", "dependencies": { "game": "1.22.0", }, }'
    meta = read_metadata(_zip(tmp_path, "genelib.zip", info))
    assert "problem" not in meta
    assert meta["mod_id"] == "genelib" and meta["game_versions"] == ("1.22.0",)


def test_commas_inside_strings_stay():
    assert _vs_json('{"a": "x,}", "b": [1, 2,],}') == {"a": "x,}", "b": [1, 2]}


def test_dependencies_are_not_a_game_version(tmp_path):
    info = '{"modid": "firearmsfork", "dependencies": {"overhaulliblegacycompat": "1.2.0"}}'
    assert read_metadata(_zip(tmp_path, "ff.zip", info))["game_versions"] == ()


def test_comments_and_raw_line_breaks_like_newtonsoft():
    text = '{\n // comment\n "modid": "x", /* block */\n "description": "line 1\nline 2",\n}'
    assert _vs_json(text) == {"modid": "x", "description": "line 1\nline 2"}


def test_url_in_string_is_not_a_comment():
    assert _vs_json('{"url": "https://example.org/a"}') == {"url": "https://example.org/a"}


def test_dependencies_as_a_list_is_a_read_error_not_a_crash(tmp_path):
    meta = read_metadata(_zip(tmp_path, "broken.zip", '{"modid": "x", "dependencies": ["game"]}'))
    assert "problem" in meta


def test_really_broken_json_stays_a_read_error(tmp_path):
    meta = read_metadata(_zip(tmp_path, "broken2.zip", '{"modid": "x" "version": "1",}'))
    assert "problem" in meta
