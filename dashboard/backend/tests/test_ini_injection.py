"""Line-break injection into INI values.

`set_ini_keys_at` writes values verbatim as a `key=value` line — a `\n` in the value would inject
arbitrary ADDITIONAL keys (env-managed/locked ones too) and thereby bypass the router key checks
(ENV_MANAGED/whitelist). The sink guard rejects that hard.
"""
import pytest

from app.services import ini_editor as ini


def _write_ini(tmp_path, content="PVP=true\n"):
    p = tmp_path / "test.ini"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_newline_in_value_rejected(tmp_path):
    path = _write_ini(tmp_path)
    with pytest.raises(ValueError):
        ini.set_ini_keys_at(path, {"PublicDescription": "Hi\nRCONPassword=evil"})
    # file unchanged — nothing was injected.
    assert ini.get_ini_at(path) == {"PVP": "true"}


def test_carriage_return_in_value_rejected(tmp_path):
    path = _write_ini(tmp_path)
    with pytest.raises(ValueError):
        ini.set_ini_keys_at(path, {"PublicDescription": "Hi\r\nOpen=true"})
    assert ini.get_ini_at(path) == {"PVP": "true"}


def test_newline_in_key_rejected(tmp_path):
    path = _write_ini(tmp_path)
    with pytest.raises(ValueError):
        ini.set_ini_keys_at(path, {"A\nPassword": "x"})
    assert ini.get_ini_at(path) == {"PVP": "true"}


def test_normal_values_still_work(tmp_path):
    path = _write_ini(tmp_path)
    ini.set_ini_keys_at(path, {"PublicDescription": "Perfectly normal text", "PVP": "false"})
    data = ini.get_ini_at(path)
    assert data["PublicDescription"] == "Perfectly normal text"
    assert data["PVP"] == "false"
