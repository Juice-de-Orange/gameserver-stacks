"""A one-key INI edit rewrites the whole file — foreign (non-UTF-8) bytes in UNINVOLVED values
must survive the round trip without loss (surrogateescape)."""
from app.services import ini_editor


def test_set_ini_keys_preserves_foreign_bytes(tmp_path):
    p = tmp_path / "server.ini"
    # 'PublicDescription' contains a cp1252 byte 0xE9 ('é'), which on its own is NOT valid UTF-8.
    p.write_bytes("PublicDescription=caf\xe9\nPVP=true\n".encode("latin-1"))

    ini_editor.set_ini_keys_at(str(p), {"PVP": "false"})  # a different key is changed

    raw = p.read_bytes()
    assert b"\xe9" in raw                 # foreign byte kept
    assert b"PVP=false" in raw            # target key changed
    assert b"PublicDescription=caf" in raw
    assert b"\xef\xbf\xbd" not in raw     # no U+FFFD (UTF-8 of the replacement char) → lossless


def test_set_ini_keys_still_rejects_newline_injection(tmp_path):
    """The injection guard stays armed (surrogateescape changes nothing about that)."""
    import pytest

    p = tmp_path / "server.ini"
    p.write_bytes(b"PVP=true\n")
    with pytest.raises(ValueError):
        ini_editor.set_ini_keys_at(str(p), {"PVP": "false\nRCONPassword=hax"})
