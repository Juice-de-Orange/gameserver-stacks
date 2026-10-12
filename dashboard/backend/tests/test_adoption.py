"""Adoption preflight: diff of the live (Compose) container against the planned recreate payload.

No DB, no Docker: `diff_specs`/`extract_*` are pure functions; the live inspect fixture mirrors a
real pz-1 container (danixu86 image, /srv/gameservers/pz-1 binds, PASSWORD set).
"""
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.services.adoption import diff_specs, extract_intended, extract_live, intended_payload


def _live_inspect(**over) -> dict:
    """Realistic /containers/pz-1/json excerpt (including env keys baked into the image,
    which the diff must ignore)."""
    env = over.pop("env", [
        "SELF_MANAGED_MODS=true",
        "RCONPASSWORD=rcon-secret",
        "MAX_MEMORY=8192m",
        "SERVERNAME=servertest",
        "ADMINUSERNAME=admin",
        "DISPLAYNAME=servertest",
        "MIN_MEMORY=2048m",
        "PUBLIC=true",
        "PASSWORD=join-secret",
        "STEAMAPPBRANCH=unstable",
        "ADMINPASSWORD=admin-secret",
        # baked into the image → must be ignored:
        "PATH=/usr/local/sbin:/usr/local/bin",
        "STEAMAPPID=380870",
        "HOME=/home/steam",
    ])
    binds = over.pop("binds", [
        "/srv/gameservers/pz-1/data:/home/steam/Zomboid:rw",
        "/srv/gameservers/pz-1/workshop-mods:/home/steam/pz-dedicated/steamapps/workshop:rw",
    ])
    base = {
        "State": {"Running": True},
        "Config": {
            "Image": over.pop("image", "danixu86/project-zomboid-dedicated-server:42.19.0-unstable"),
            "Env": env,
            "ExposedPorts": over.pop("exposed", {"16261/udp": {}, "16262/udp": {}, "27015/tcp": {}}),
        },
        "HostConfig": {
            "NetworkMode": "gameserver-net",
            "Binds": binds,
            "PortBindings": {
                "16261/udp": [{"HostPort": "16261"}],
                "16262/udp": [{"HostPort": "16262"}],
            },
            "Memory": 0,
            "NanoCpus": 0,
        },
    }
    base.update(over)
    return base


def _server(**over) -> GameServer:
    base = dict(
        key="pz-1",
        game_type=GameType.pz,
        display_name="servertest",
        management_mode=ManagementMode.compose,  # as live; intended_payload flips it on a copy
        container_name="pz-1",
        data_dir="/srv/gameservers/pz-1",
        image_repo="danixu86/project-zomboid-dedicated-server",
        image_tag="42.19.0-unstable",
        console_kind=ConsoleKind.source_rcon,
        rcon_port=27015,
        rcon_password="rcon-secret",  # literal fallback for resolve_secret (test-local)
        game_ports='{"game":"16261/udp","direct":"16262/udp"}',
        ram_mb=0,
        cpu_cores=0.0,
        heap_max_mb=8192,
        heap_min_mb=2048,
        presence_visible=True,
        extra='{"servername":"servertest","admin_user":"admin","password":"join-secret"}',
    )
    base.update(over)
    return GameServer(**base)


def test_faithful_recreate_has_no_critical():
    """Registry row correctly reconciled (PASSWORD via extra) → faithful recreate, no critical findings."""
    live = extract_live(_live_inspect())
    intended = extract_intended(intended_payload(_server()))
    findings = diff_specs(live, intended)
    crit = [f for f in findings if f["severity"] == "critical"]
    assert crit == [], crit


def test_missing_join_password_is_critical():
    """Without a PASSWORD source in the row the public server would be recreated without a password."""
    live = extract_live(_live_inspect())
    intended = extract_intended(intended_payload(_server(extra='{"servername":"servertest"}')))
    findings = diff_specs(live, intended)
    pw = [f for f in findings if f["field"] == "env:PASSWORD"]
    assert len(pw) == 1 and pw[0]["severity"] == "critical"
    assert pw[0]["live"] == "set" and pw[0]["intended"] == "MISSING"


def test_bind_mismatch_is_critical():
    """A live world bind that the recreate does not cover is critical (the world would be missing)."""
    live = extract_live(_live_inspect(binds=[
        "/srv/gameservers/pz-1/OTHER-WORLD:/home/steam/Zomboid:rw",
        "/srv/gameservers/pz-1/workshop-mods:/home/steam/pz-dedicated/steamapps/workshop:rw",
    ]))
    intended = extract_intended(intended_payload(_server()))
    findings = diff_specs(live, intended)
    binds = [f for f in findings if f["field"] == "binds" and f["severity"] == "critical"]
    assert binds, findings


def test_image_mismatch_is_critical():
    live = extract_live(_live_inspect(image="danixu86/project-zomboid-dedicated-server:41.78-stable"))
    intended = extract_intended(intended_payload(_server()))
    findings = diff_specs(live, intended)
    assert any(f["field"] == "image" and f["severity"] == "critical" for f in findings)


def test_secret_values_never_leak_into_findings():
    """Secret VALUES must never show up in findings (only 'set'/'MISSING')."""
    live = extract_live(_live_inspect())
    intended = extract_intended(intended_payload(_server(extra='{"servername":"servertest"}')))
    blob = repr(diff_specs(live, intended))
    for secret in ("join-secret", "rcon-secret", "admin-secret"):
        assert secret not in blob
