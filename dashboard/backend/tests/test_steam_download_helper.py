"""The workshop download runs in a short-lived steamcmd helper container, not in the API image.

What must hold: the helper is only pulled when it is missing, runs as the API's own user with
nothing but the cache mounted, always logs in anonymously (the entrypoint takes two numbers),
and the path it reports is only followed inside that cache.
"""
import asyncio
import os
from pathlib import Path

from app.config import settings
from app.models.server import GameServer, GameType
from app.services import game_settings
from app.services import steam_download as sd


class _Docker:
    """Socket-proxy stub: records the calls, the helper "runs" once and writes its item."""

    def __init__(self, cache: str, *, has_image=True, pull_fails=False, exit_code=0, log=None,
                 runs_forever=False):
        self.cache = cache
        self.has_image = has_image
        self.pull_fails = pull_fails
        self.exit_code = exit_code
        self.log = log
        self.runs_forever = runs_forever
        self.calls: list[tuple] = []

    async def image_exists(self, image):
        self.calls.append(("image_exists", image))
        return self.has_image

    async def pull(self, image, on_progress=None):
        self.calls.append(("pull", image))
        if self.pull_fails:
            raise RuntimeError("pull ghcr.io/x → 404: not found")

    async def remove(self, name, force=False):
        self.calls.append(("remove", name))

    async def create(self, name, payload):
        self.calls.append(("create", name, payload))
        return {"Id": "abc"}

    async def action(self, name, act, **kw):
        self.calls.append(("action", name, act))
        # What steamcmd does inside the helper: the item lands in the cache mount.
        item = os.path.join(self.cache, "Steam/steamapps/workshop/content/108600/123")
        os.makedirs(item, exist_ok=True)
        with open(os.path.join(item, "mod.info"), "w", encoding="utf-8") as f:
            f.write("id=Demo\n")

    async def state(self, name):
        return {"exists": True, "running": self.runs_forever}

    async def logs(self, name, tail=200, **kw):
        return self.log if self.log is not None else (
            'Success. Downloaded item 123 to "/steam/Steam/steamapps/workshop/content/108600/123" (8 bytes)'
        )

    async def inspect(self, name):
        return {"State": {"ExitCode": self.exit_code}}


def _setup(monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "steamcmd_image", "local/steamcmd:test")
    monkeypatch.setattr(game_settings, "is_active", lambda game, session=None: True)
    fake = _Docker(str(tmp_path / ".steamcmd"), **kw)
    monkeypatch.setattr(sd, "docker", fake)
    monkeypatch.setattr(sd, "_helper_lock", None)
    server = GameServer(key="pz-1", game_type=GameType.pz, data_dir=str(tmp_path / "pz-1"))
    return fake, server


def test_the_container_spec_is_the_helper_and_nothing_more():
    spec = sd.helper_spec(108600, "123", "/data/games/.steamcmd", 1000, 1001, "img:1")
    assert spec["Image"] == "img:1"
    assert spec["Cmd"] == ["108600", "123"]               # the entrypoint adds +login anonymous
    assert spec["User"] == "1000:1001"
    assert spec["Env"] == ["HOME=/steam"]
    host = spec["HostConfig"]
    assert host["Binds"] == ["/data/games/.steamcmd:/steam"]
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert not host.get("Privileged") and "NetworkMode" not in host and "PortBindings" not in host


def test_a_download_runs_the_helper_and_mirrors_the_item(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path)
    result = asyncio.run(sd.download_item("123", server))
    assert result["ok"], result
    assert os.path.isfile(os.path.join(result["path"], "mod.info"))
    assert result["path"] == str(tmp_path / "pz-1/workshop-mods/content/108600/123")

    created = [c for c in fake.calls if c[0] == "create"]
    assert len(created) == 1
    name, payload = created[0][1], created[0][2]
    assert name == f"console-steamcmd-{sd.install_id()}-123"
    assert payload["Image"] == "local/steamcmd:test"
    assert payload["User"] == f"{os.getuid()}:{os.getgid()}"
    assert payload["HostConfig"]["Binds"] == [f"{tmp_path}/.steamcmd:/steam"]
    assert ("pull", "local/steamcmd:test") not in fake.calls   # present → not pulled again
    assert fake.calls[-1] == ("remove", sd.helper_name("123"))  # nothing left behind
    assert os.path.isdir(tmp_path / ".steamcmd")                 # the cache, created by the API


def test_a_missing_helper_image_is_pulled_first(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path, has_image=False)
    assert asyncio.run(sd.download_item("123", server))["ok"]
    ops = [c[0] for c in fake.calls]
    assert ops.index("pull") < ops.index("create")


def test_a_failed_pull_says_how_to_build_the_image(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path, has_image=False, pull_fails=True)
    result = asyncio.run(sd.download_item("123", server))
    assert not result["ok"]
    assert "docker build -t local/steamcmd:test dashboard/images/steamcmd" in result["log"]
    assert not any(c[0] == "create" for c in fake.calls)


def test_a_failed_steamcmd_run_reports_its_output(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path, exit_code=8, log="ERROR! Download item 123 failed (Failure).")
    result = asyncio.run(sd.download_item("123", server))
    assert not result["ok"]
    assert "failed (Failure)" in result["log"]


def test_a_hanging_helper_is_removed_after_the_timeout(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path, runs_forever=True)
    result = asyncio.run(sd.download_item("123", server, timeout=0.01))
    assert not result["ok"] and "Timed out" in result["log"]
    assert fake.calls[-1] == ("remove", sd.helper_name("123"))


def test_a_reported_path_outside_the_cache_is_not_followed(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path, log='Downloaded item 123 to "/etc/passwd-dir"')
    result = asyncio.run(sd.download_item("123", server))
    assert not result["ok"]
    assert sd.host_path("/steam/../etc", "/c") is None
    assert sd.host_path("/steamy/x", "/c") is None
    assert sd.host_path("/steam/Steam/x", "/c") == "/c/Steam/x"


def test_a_cache_path_that_leads_out_of_the_cache_by_symlink_is_not_followed(monkeypatch, tmp_path):
    """The reported path is lexically inside the cache, but a folder on the way is a symlink the
    helper planted — the item then really lies outside."""
    fake, server = _setup(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    cache = tmp_path / ".steamcmd"
    cache.mkdir()
    os.symlink(outside, cache / "Steam")

    result = asyncio.run(sd.download_item("123", server))

    assert not result["ok"] and "outside the steamcmd cache" in result["log"]
    assert not os.path.exists(tmp_path / "pz-1" / "workshop-mods")


def test_an_inactive_game_downloads_nothing(monkeypatch, tmp_path):
    fake, server = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(game_settings, "is_active", lambda game, session=None: False)
    result = asyncio.run(sd.download_item("123", server))
    assert not result["ok"]
    assert result["log"] == "This game is not active on this installation (Settings → Games)."
    assert fake.calls == []


def test_the_helper_image_is_a_setting(monkeypatch):
    from app.config import Settings

    assert settings.steamcmd_image                      # a default tag, never empty
    monkeypatch.setenv("GSS_STEAMCMD_IMAGE", "local/steamcmd:1")
    assert Settings(_env_file=None).steamcmd_image == "local/steamcmd:1"


def test_the_api_image_carries_no_steamcmd():
    """steamcmd and its 32-bit libraries come from the helper image, on demand only."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    for word in ("steamcmd", "i386", "lib32"):
        assert word not in code, word
    helper = Path(__file__).resolve().parents[2] / "images" / "steamcmd"
    assert "+login anonymous" in (helper / "entrypoint.sh").read_text(encoding="utf-8")


def test_stale_helpers_are_removed_at_start_and_nothing_else(monkeypatch, tmp_path):
    """An API crash in the middle of a download leaves its helper behind (the `finally` that
    removes it never ran). The start clears exactly this installation's name pattern (and the
    old form without an installation id) — and only helpers on this installation's cache
    (`console-steamcmd-789` belongs to another one)."""
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    removed: list[str] = []
    ours = f"console-steamcmd-{sd.install_id()}-123"

    class _Dk:
        async def inspect(self, name):
            cache = "/other/root/.steamcmd" if name.endswith("789") else str(tmp_path / ".steamcmd")
            return {"HostConfig": {"Binds": [f"{cache}:/steam"]}}

        async def container_names(self, name_filter):
            assert name_filter == sd.HELPER_PREFIX
            # Docker's name filter is a substring match — the near misses must survive, and so
            # must a helper with another installation's id.
            return [ours, "console-steamcmd-456", "console-steamcmd-789",
                    "console-steamcmd-0badc0de-123", "my-console-steamcmd-1",
                    "console-steamcmd-cache", "console-steamcmd-7-old", "console-api"]

        async def remove(self, name, force=False):
            assert force, "a leftover may still be running"
            removed.append(name)

    monkeypatch.setattr(sd, "docker", _Dk())
    assert asyncio.run(sd.remove_stale_helpers()) == sorted([ours, "console-steamcmd-456"])
    assert removed == sorted([ours, "console-steamcmd-456"])


def test_two_installations_name_their_helpers_apart(monkeypatch, tmp_path):
    """Container names are host-wide: with the item id alone, installation B's "remove the
    leftover of an aborted run" removed installation A's running helper for the same item."""
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "a"))
    monkeypatch.setattr(settings, "game_network", "net-a")
    a = sd.helper_name("123")
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "b"))
    monkeypatch.setattr(settings, "game_network", "net-b")
    b = sd.helper_name("123")
    assert a != b and a.startswith(sd.HELPER_PREFIX) and b.endswith("-123")
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "a"))
    monkeypatch.setattr(settings, "game_network", "net-a")
    assert sd.helper_name("123") == a, "the id must be stable across restarts"


def test_the_start_cleanup_never_stops_the_start(monkeypatch):
    class _Dk:
        async def container_names(self, name_filter):
            raise RuntimeError("socket proxy not reachable")

    monkeypatch.setattr(sd, "docker", _Dk())
    assert asyncio.run(sd.remove_stale_helpers()) == []


def test_the_api_start_runs_the_cleanup():
    import inspect

    from app import main

    assert "remove_stale_helpers" in inspect.getsource(main.lifespan)
