"""backup.py parametrisation + PZ default shim (tmp_path, no live state)."""
import os
import tarfile

from app.services import backup as bk


def _world(root: str) -> None:
    for sub in ("Saves", "Server"):
        d = os.path.join(root, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as f:
            f.write(sub)


def test_parametrized_create_contains_world(tmp_path):
    data = tmp_path / "data"
    _world(str(data))
    calls = []
    res = bk.create_backup(
        "manual",
        data_dir=str(data),
        world_subdirs=("Saves", "Server"),
        pre_hook=lambda: calls.append("pre"),
        post_hook=lambda: calls.append("post"),
    )
    assert calls == ["pre", "post"]  # post fires on success too (finally)
    arc = data / "backups" / res["name"]
    assert arc.exists()
    with tarfile.open(arc, "r:gz") as tar:
        tops = {n.split("/")[0] for n in tar.getnames()}
    assert {"Saves", "Server"} <= tops


def test_extract_roundtrip(tmp_path):
    data = tmp_path / "data"
    _world(str(data))
    res = bk.create_backup("manual", data_dir=str(data))
    # Delete the world, then restore it from the archive.
    for sub in ("Saves", "Server"):
        os.remove(os.path.join(str(data), sub, "f.txt"))
    bk.extract_backup(res["name"], data_dir=str(data))
    assert os.path.exists(os.path.join(str(data), "Saves", "f.txt"))
    assert os.path.exists(os.path.join(str(data), "Server", "f.txt"))
