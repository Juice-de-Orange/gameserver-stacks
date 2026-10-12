"""Join overview per server — for friends, not for admins.

Shows how to get onto THIS server, whether it is running and when the next restart is due.
No password is returned.

Everything comes from `driver.connect_info(server)` and the registry — nothing about address,
port or server name is hard-coded, so the page works for every game.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..auth.deps import get_current_user, get_server
from ..config import settings
from ..errors import err
from ..models import User
from ..models.server import GameServer
from ..services import mod_check, player_count
from ..services import restarts
from ..services.docker_client import docker
from ..services.drivers import get_driver, running_version
from ..services.mods import state as mod_state
from ..services.rcon import RconError

router = APIRouter(prefix="/api/servers", tags=["join"])


async def _pz_mods(server: GameServer) -> list[dict]:
    """Workshop mods with title and link — only PZ downloads them automatically on join."""
    wids = [str(x) for x in mod_state.live_plan(server).workshop_items]
    titles: dict[str, str] = {}
    report = mod_check.cached(server.key)
    if report:
        titles = {it["workshop_id"]: it["title"] for it in report.get("items", [])}
    elif wids:
        steam = await mod_check.fetch_steam(wids)
        titles = {k: (v.get("title") or "") for k, v in steam.items()}
    return [
        {"workshop_id": w, "title": titles.get(w) or f"Workshop {w}", "url": mod_check.workshop_url(w)}
        for w in wids
    ]


def _other_mods(server: GameServer, driver) -> list[dict]:
    """For MC/VS/Reforger: the installed mods/plugins as the driver knows them."""
    try:
        data = driver.list_mods(server)
    except Exception:
        return []
    # The drivers return `mods` and (for Minecraft) `plugins` — none of them has `items`.
    # Reading the wrong key yields None, and the page then makes a positively false statement
    # ("active mods (0) — the server runs without additional content") without any error.
    #
    # **Only `mods`, explicitly NOT `plugins`.** This page answers "what do I need to join" and
    # is open to every signed-in user (no permission flag). `data/mods` is client-side content
    # a player downloads anyway; `data/plugins` holds server-side Paper plugins with the exact
    # version in the file name — admin, anti-cheat and moderation tools. The same list sits
    # behind `can_manage_mods` in `GET /{key}/mods`, which neither `member` nor `trusted` has.
    # Returning it here would bypass that restriction.
    items = data.get("mods") if isinstance(data, dict) else data
    out: list[dict] = []
    for it in items or []:
        if isinstance(it, dict):
            # `modId` is Reforger's key — there is no `id` there. Without this branch an entry
            # without `name` (the normal case when adding by workshop GUID) would fall back to
            # `str(it)` and the UI would show the raw Python dict.
            title = str(it.get("name") or it.get("modId") or it.get("id") or it)
        else:
            title = str(it)
        out.append({"workshop_id": "", "title": title, "url": ""})
    return out


MODPACK_DIR = Path(__file__).resolve().parent.parent / "modpacks"


def _modpack(server: GameServer) -> Path | None:
    """The join pack for the version this server is running RIGHT NOW — or nothing.

    Looks for `stacks-<game_version>.mrpack`. That way the page cannot offer a dead link: after
    a version jump without a new pack the button disappears instead of handing out a 404.

    The version comes from the registry and therefore from user input — so it is not put into
    a path without being validated first.
    """
    if server.game_type.value != "minecraft":
        return None
    version = running_version(server).strip()
    if not version or not re.fullmatch(r"[0-9A-Za-z._-]+", version):
        return None
    path = MODPACK_DIR / f"stacks-{version}.mrpack"
    return path if path.is_file() else None


def _has_voice_chat(server: GameServer, driver) -> bool | None:
    """Is a proximity voice chat running on this server? `None` = **unknown**.

    Deliberately only a boolean and explicitly NOT the plugin list: that is withheld a few
    lines above because, with Paper, `data/plugins` contains the admin, anti-cheat and
    moderation tools including their exact versions. That a voice chat exists is something a
    player does need to know — otherwise they do not know why they need a modpack.

    `None` instead of `False` when the folder cannot be read. The page then says nothing
    instead of claiming "no voice chat".
    """
    try:
        data = driver.list_mods(server)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    names = [str(x.get("name") if isinstance(x, dict) else x).lower()
             for x in (data.get("plugins") or [])]
    return any(n.startswith("voicechat") for n in names)


def _client_setup(server: GameServer, driver) -> dict | None:
    """What the PLAYER needs on their own machine — only where it is not self-explanatory.

    Minecraft is the case this exists for, for three reasons that each lock players out:

    * **Java, not Bedrock.** The Microsoft Store edition speaks a different protocol and
      supports neither plugins nor voice chat.
    * **The exact version matters.** A client on 26.2 cannot join a 26.1.2 server; the in-game
      error just says "Outdated client".
    * **Proximity chat needs a client mod.** On the server it runs as a plugin, but a vanilla
      client hears nothing — the only symptom is that everyone is silent.
    """
    if server.game_type.value != "minecraft":
        return None
    return {
        "edition": "Minecraft: Java Edition",
        "version": running_version(server),
        "voice": _has_voice_chat(server, driver),
        "modpack": _modpack(server) is not None,
    }


@router.get("/{key}/join-pack")
def join_pack(
    server: GameServer = Depends(get_server),
    _: User = Depends(get_current_user),
) -> FileResponse:
    """Serve the join pack — same permission level as the join page itself.

    It contains no secrets: loader, game version and the source URLs of two public mods. It
    still requires a login because the whole page does.
    """
    path = _modpack(server)
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err(
                "join.pack_missing", "There is no join pack for the running version ({version}).",
                version=running_version(server) or "unknown",
            ),
        )
    return FileResponse(path, filename=path.name, media_type="application/x-modrinth-modpack+zip")


@router.get("/{key}/join-info")
async def join_info(
    server: GameServer = Depends(get_server),
    _: User = Depends(get_current_user),
) -> dict:
    driver = get_driver(server)
    conn = driver.connect_info(server)

    # Live status — every source guarded individually, the page must never return a 500.
    running, players, max_players = False, None, None
    try:
        running = bool((await docker.state(server.container_name)).get("running"))
    except Exception:
        running = False
    if running:
        try:
            players = (await player_count.players(server, driver))[0]["count"]
        except (RconError, Exception):
            players = None
    try:
        max_players = driver.overview_stats(server).get("max_players")
    except Exception:
        max_players = None

    mods = await _pz_mods(server) if server.game_type.value == "pz" else _other_mods(server, driver)

    return {
        "server": {
            "key": server.key,
            "name": conn.browser_name,
            "host": conn.host,
            "port": conn.port,
            "protocol": conn.protocol,
            "uri": conn.uri,
            "password_required": conn.password_required,
            "hint": conn.hint,
        },
        "status": {"running": running, "players": players, "max_players": max_players},
        "next_restart": restarts.next_restart(server_id=server.id) if server.id is not None else None,
        "mods": mods,
        # Both are PZ convenience links (Steam Workshop and community) — the other games have
        # no equivalent, so they are omitted there instead of shown misleadingly.
        "mod_collection_url": (settings.mod_collection_url or None) if server.game_type.value == "pz" else None,
        "community_url": settings.community_url or None,
        "auto_download": server.game_type.value == "pz",
        # What the player needs on THEIR machine (Minecraft: edition, version, voice chat).
        "client_setup": _client_setup(server, driver),
    }
