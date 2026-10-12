"""Command-line commands for operations — run them on the host, in the `dashboard/` directory:
`docker compose exec console-api python -m app.cli …` (`console-api` is the compose service; add
`-p <name>` if the stack runs under a project name of its own, and `-T` where there is no
terminal, as in a systemd unit).

The main reason for this file is the **host reboot**. When the host shuts down, no dashboard
code runs; dockerd stops the containers itself and gives them its own `--shutdown-timeout`
(default **15 s**), regardless of what is configured on the container. For Project Zomboid that
is fatal twice over: 15 s are not enough, and the signal would never arrive anyway
(`services/lifecycle.py` explains why).

The solution is a systemd unit on the host that is stopped **before** `docker.service` and calls
`stop-all` — exactly the same path the dashboard uses. At the next boot `start-recorded` brings
back the servers that were running before. `dashboard/examples/systemd/stacks-console.service`
is such a unit: it calls `docker compose exec -T console-api python -m app.cli stop-all` and
`… start-recorded`. (The game blocks have their own unit in the root `examples/systemd/`.)

Why the running servers have to be recorded at all: the clean shutdown goes through
`docker stop`, after which the container counts as *manually stopped* — `unless-stopped` will not
bring it up again after the reboot. Without this file every game server would stay down after
each reboot.

No dockerd restart required.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger("app.cli")

# Lives on the `/app/data` volume of the compose file, so it survives a rebuild of the API
# container.
RECORD_PATH = os.environ.get("GSS_GRACEFUL_RECORD", "/app/data/graceful-running.json")


async def _stop_all(record_path: str) -> int:
    from .services import lifecycle
    from .services import servers as servers_registry
    from .services.docker_client import docker

    running: list[str] = []
    for server in servers_registry.list_enabled():
        try:
            if (await docker.state(server.container_name)).get("running"):
                running.append(server.key)
        except Exception as e:
            # **When in doubt the server counts as RUNNING** — the same rule as in
            # `lifecycle.stop_and_confirm`, for the same reason. Skipping it here would take
            # away the very path this file exists for: dockerd kills it seconds later with its
            # own 15 s timeout, for PZ SIGTERM never arrives, and the world falls back to the
            # last autosave. A `stop_server` on an already stopped container is harmless.
            print(f"  {server.key}: state not readable ({e}) — counts as running", flush=True)
            running.append(server.key)

    # Record FIRST, then stop: if the run is interrupted halfway, the list is still complete
    # and the next start brings everything back.
    #
    # **If recording fails, nothing is stopped at all.** The stop goes through `docker stop`,
    # the container then counts as manually stopped, and `unless-stopped` does not bring it
    # back at boot — the record is the ONLY way back. Without it every game server would stay
    # down after the reboot; by aborting here dockerd takes over and the restart policy
    # brings them back by itself.
    if not _write_record(record_path, running):
        print("ABORTED: the record could not be written — NOTHING is stopped.",
              flush=True)
        print("         dockerd stops the containers itself; their restart policy brings them back.",
              flush=True)
        return 1
    print(f"running game servers: {running or '—'}", flush=True)

    for key in running:
        server = servers_registry.get_by_key(key)
        if server is None:
            continue
        print(f"  {key}: stopping…", flush=True)
        try:
            await lifecycle.stop_server(server, announce_text="The server is shutting down (maintenance).")
            print(f"  {key}: stopped", flush=True)
        except Exception as e:
            print(f"  {key}: ERROR while stopping: {e}", flush=True)
    return 0


async def _start_recorded(record_path: str) -> int:
    from .services import servers as servers_registry
    from .services.docker_client import docker

    keys = _read_record(record_path)
    if not keys:
        print("nothing to start (no recorded servers)", flush=True)
        return 0
    print(f"recorded servers: {keys}", flush=True)
    open_: list[str] = []
    for key in keys:
        server = servers_registry.get_by_key(key)
        if server is None:
            print(f"  {key}: no longer exists — skipped", flush=True)
            continue
        try:
            await docker.action(server.container_name, "start")
            print(f"  {key}: started", flush=True)
        except Exception as e:
            # **A failed start stays recorded.** Dropping it here would throw away the only
            # source for "this server was running before the reboot": the unit calls
            # `start-recorded` exactly ONCE at boot, the watchdog never starts anything on its
            # own, and `docker stop` has disarmed `unless-stopped`. The server would stay down
            # for good, with the only hint in the journal.
            print(f"  {key}: ERROR while starting: {e} — stays recorded", flush=True)
            open_.append(key)
    # Only rewrite after the loop — a crash halfway must not lose the rest.
    _write_record(record_path, open_)
    if open_:
        print(f"NOT started and still recorded: {open_}", flush=True)
        print("         try again: docker compose exec console-api python -m app.cli start-recorded",
              flush=True)
        return 1
    return 0


def _write_record(path: str, keys: list[str]) -> bool:
    """Write the record. Returns WHETHER it worked — and the caller acts on it.

    For `stop-all` a failure means the way back is missing, not just a line in the journal.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"running": keys}, f)
        return True
    except OSError as e:
        print(f"WARNING: record not writable ({e})", flush=True)
        return False


def _read_record(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [str(k) for k in json.load(f).get("running", [])]
    except (OSError, ValueError):
        return []


def _backfill(server_key: str) -> int:
    """Backfill events from the ARCHIVED log files and enrich sessions with the account ID.

    The live parser only sees the current day's file. Everything before that — months of joins,
    leaves and deaths — sits unused in `data/Logs/logs_<date>/`. Without it the leaderboard can
    list the same person under two spellings of their name, and the death map stays empty.

    Idempotent: duplicate events are detected by (name, kind, time, server), and a session only
    gets its ID if it has none yet. Safe to run repeatedly.
    """
    import datetime as dt
    import glob
    import os as _os

    from sqlmodel import Session, select

    from .db import engine
    from .models import PlayerEvent, PlayerSession
    from .services import servers as servers_registry
    from .services.drivers import get_driver

    server = servers_registry.get_by_key(server_key)
    if server is None:
        print(f"Server {server_key!r} does not exist")
        return 2
    drv = get_driver(server)

    pattern = _os.path.join(server.data_dir, "data", "Logs", "**", "*_user.txt")
    pattern2 = _os.path.join(server.data_dir, "data", "Logs", "**", "*_PerkLog.txt")
    files = sorted(glob.glob(pattern, recursive=True) + glob.glob(pattern2, recursive=True))
    print(f"{len(files)} log files found")
    if not files:
        return 0

    text = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text.append(f.read())
        except OSError as e:
            print(f"  {path}: not readable ({e})")
    events = drv.parse_events(server, "\n".join(text))
    print(f"{len(events)} events parsed")

    new = 0
    with Session(engine) as session:
        cond = servers_registry.scope_clause(PlayerEvent.server_id, server)
        for ev in events:
            existing = session.exec(select(PlayerEvent).where(
                PlayerEvent.username == ev.username, PlayerEvent.kind == ev.kind,
                PlayerEvent.at == ev.at, cond,
            )).first()
            if existing:
                continue
            session.add(PlayerEvent(
                username=ev.username, kind=ev.kind, at=ev.at, server_id=server.id,
                detail=ev.detail[:500], game_id=ev.game_id, x=ev.x, y=ev.y, z=ev.z,
            ))
            new += 1
        session.commit()
    print(f"{new} new events stored")

    # Enrich sessions: the join from the log is close to the session start the poll saw
    # (± poll interval). Without an ID the session stays as it was — no guessing.
    window = dt.timedelta(minutes=30)
    added = 0
    with Session(engine) as session:
        s_cond = servers_registry.scope_clause(PlayerSession.server_id, server)
        e_cond = servers_registry.scope_clause(PlayerEvent.server_id, server)
        open_ = session.exec(select(PlayerSession).where(PlayerSession.steam_id == "", s_cond)).all()
        for s in open_:
            ev = session.exec(select(PlayerEvent).where(
                PlayerEvent.username == s.username, PlayerEvent.kind == "join",
                PlayerEvent.game_id != "",
                PlayerEvent.at >= s.joined_at - window,
                PlayerEvent.at <= s.joined_at + window,
                e_cond,
            ).order_by(PlayerEvent.at)).first()
            if ev:
                s.steam_id = ev.game_id
                session.add(s)
                added += 1
        session.commit()
    print(f"{added} sessions enriched with the game account ID")
    return 0



def _fix_links(write: bool) -> int:
    """Set account links to the correct game — dry run by default.

    `player_links.game` defaults to `"pz"`, and links created without an explicit game are all
    `pz`, even those of Minecraft, Vintage Story or Reforger players.
    `routers/stats._game_scope` then filters on the servers of THAT game; for the others
    "my stats" stays empty for good, and the UI offers no way to correct it.

    The game is derived from the real `player_events`: if the linked name appears only on servers
    of ONE game, that is the answer. If it appears on several or on none, the link is **not**
    touched and is listed instead — guessing would be worse than doing nothing, because a wrong
    assignment credits someone else's playtime to this account.
    """
    from sqlmodel import Session, select

    from .db import engine
    from .models.player import PlayerEvent, PlayerLink
    from .models.server import GameServer

    with Session(engine) as s:
        server_game = {sv.id: sv.game_type.value for sv in s.exec(select(GameServer)).all()}
        links = s.exec(select(PlayerLink)).all()
        if not links:
            print("No links present — nothing to do.")
            return 0

        changes: list[tuple[PlayerLink, str]] = []
        unclear: list[tuple[PlayerLink, str]] = []
        for link in links:
            hits = s.exec(
                select(PlayerEvent).where(PlayerEvent.username == link.ingame_username)
            ).all()
            games = {server_game.get(e.server_id) for e in hits if e.server_id is not None}
            games.discard(None)
            if len(games) == 1:
                target = next(iter(games))
                if target != link.game:
                    changes.append((link, target))
            elif not games:
                unclear.append((link, "no events for this name"))
            else:
                unclear.append((link, f"name appears in several games: {sorted(games)}"))

        print(f"{len(links)} links checked.\n")
        if changes:
            print("Would be changed:")
            for link, target in changes:
                print(f"  #{link.id:<4} {link.ingame_username:<24} {link.game!r} → {target!r}")
        else:
            print("Nothing to change — every assignment matches the events.")
        if unclear:
            print("\nNOT touched (guessing would be worse than doing nothing):")
            for link, reason in unclear:
                print(f"  #{link.id:<4} {link.ingame_username:<24} {link.game!r}  — {reason}")

        if not write:
            print("\nDry run. To write:  python -m app.cli fix-links --write")
            return 0
        if not changes:
            return 0
        for link, target in changes:
            link.game = target
            s.add(link)
        s.commit()
        print(f"\n{len(changes)} links written.")
        return 0


def _smoke_token(username: str | None) -> int:
    """Issue an access token for acceptance checks — without password or login throttle.

    **The output is the contract:** stdout carries the token and NOTHING else, because callers
    read it straight into a variable. Every explanation goes to stderr; on failure stdout stays
    empty so that a caller does not mistake garbage for a token.

    Why not a login: the seed password in `.env` may be long outdated, every failed attempt
    counts towards the login throttle and locks out exactly the account the operator needs, and
    every run would leave a sign-in in the audit log.

    No new trust boundary: whoever can run this command has Docker access to the host and
    therefore `JWT_SECRET` anyway.
    """
    from sqlmodel import Session, select

    from .auth.security import make_access_token
    from .config import jwt_secret_problem, settings
    from .db import engine
    from .models import User

    # The same check the API runs at start (`main.lifespan`): a token signed with the public
    # default or a short secret is one anybody could have forged.
    if (problem := jwt_secret_problem(settings.jwt_secret)):
        print(f"No token issued: {problem}", file=sys.stderr)
        return 1
    name = username or settings.seed_admin_username
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == name)).first()
        if user is None or user.id is None:
            print(f"No account named {name!r} — no token issued.", file=sys.stderr)
            return 1
        print(make_access_token(user.id, user.token_version))
    return 0



async def _create_container(server_key: str) -> int:
    """Create the container of a registry server for the FIRST time.

    The same service the "Create container" button runs (`services/provisioning.py`, which
    explains the order: checks, image, data directories, configuration, container). This command
    stays for scripted installs and for a host without a browser at hand.
    """
    from fastapi import HTTPException

    from .services import provisioning

    class _Say:
        """Prints each step once; the download as whole percent steps of ten."""

        def __init__(self) -> None:
            self.last = ""
            self.tenth = -1

        def step(self, text: str, *, current: int = 0, total: int = 0) -> None:
            if text != self.last:
                print(text, flush=True)
                self.last = text
            if total:
                tenth = current * 10 // total
                if tenth != self.tenth:
                    self.tenth = tenth
                    print(f"  {tenth * 10} %", flush=True)

    try:
        result = await provisioning.provision(server_key, progress=_Say())
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"code": "", "message": str(e.detail)}
        code, message = detail.get("code", ""), detail.get("message", "")
        params = detail.get("params") or {}
        if code == "server.not_found":
            print(f"ERROR: server '{server_key}' is not in the registry.")
            return 2
        if code == "provision.compose_managed":
            print(f"ERROR: '{server_key}' is compose-managed — here Compose creates the container, "
                  f"not the dashboard.")
            return 2
        if code == "provision.container_exists":
            # NO silent overwrite: an existing container is a running game or a stopped one with
            # a world next to it. Replacing it is the job of the version change.
            print(f"'{params.get('container', '')}' already exists ({params.get('state', '?')}) — "
                  f"nothing done. To replace it: version change in the dashboard.")
            return 0
        if code == "provision.pull_failed":
            print(f"ERROR: {params.get('image', '')} could not be pulled ({params.get('reason', '')}).")
            return 1
        if code == "provision.config_failed":
            print(f"ERROR: the configuration for '{server_key}' could not be created: {params.get('reason', '')}")
            print("       No container created — otherwise the server would start without a control channel.")
            return 1
        print(f"ERROR: {message}")
        return 2 if code == "provision.eula_required" else 1
    print(f"Container '{result['container']}' created (image {result['image']}). "
          f"Start it from the dashboard.")
    return 0


def _remove_server(key: str, write: bool, include_active: bool = False) -> int:
    """Remove a registry row together with its telemetry. Dry run unless `--write`.

    The database part of "Remove" in the dashboard (`services/provisioning.delete_server_rows`,
    which explains what is deleted and what stays). This command touches neither the container
    nor the data directory — the dashboard's "Remove" does both.

    **An enabled server is refused.** The command is irreversible and a typo (`pz-1` instead of
    `pz-test`) would take the production server out of the registry. Whoever really wants that
    adds `--include-active`.
    """
    from sqlmodel import Session, select

    from .db import engine
    from .models import GameServer
    from .services import provisioning

    with Session(engine) as s:
        server = s.exec(select(GameServer).where(GameServer.key == key)).first()
        if server is None:
            print(f"No server with the key {key!r} — nothing to do.")
            return 0
        sid, name, enabled = server.id, server.display_name, server.enabled

    print(f"Server {key!r} (id={sid}, {name!r}, enabled={enabled})")
    unknown = provisioning.unclassified_tables()
    if unknown:
        print("ABORTED: these per-server tables are listed neither as telemetry nor as")
        print(f"         records: {', '.join(unknown)} (NOT CLASSIFIED)")
        print("         Add them to `provisioning.TELEMETRY` or `provisioning.RECORDS` — an")
        print("         irreversible command does not guess.")
        return 2
    counts = provisioning.row_counts(sid)
    for tbl, n in counts.items():
        outcome = "STAYS (record)" if tbl in provisioning.RECORDS else "will be deleted"
        print(f"  {tbl:22s} {n:6d} rows  — {outcome}")
    if not counts:
        print("  (no dependent rows)")

    if not write:
        print()
        print("DRY RUN — nothing changed. Run with --write to write.")
        return 0

    # The guard sits AFTER the dry run, not before it. The other way round one would have to
    # type `--include-active` just to have a look — training exactly the habit the guard is
    # built against.
    if enabled and not include_active:
        print()
        print("ABORTED: this server is ENABLED. Removing it cannot be undone.")
        print("         Disable it in the dashboard first — or add `--include-active`.")
        return 2

    provisioning.delete_server_rows(sid)
    print()
    print(f"Removed: registry row {key!r} and its telemetry.")
    print(f"Left unchanged: {', '.join(sorted(provisioning.RECORDS))}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else ""
    if cmd == "stop-all":
        return asyncio.run(_stop_all(args[1] if len(args) > 1 else RECORD_PATH))
    if cmd == "start-recorded":
        return asyncio.run(_start_recorded(args[1] if len(args) > 1 else RECORD_PATH))
    if cmd == "backfill-events":
        if len(args) < 2:
            print("ERROR: backfill-events needs a server key.")
            return 2
        return _backfill(args[1])
    if cmd == "fix-links":
        return _fix_links("--write" in args)
    if cmd == "smoke-token":
        return _smoke_token(args[1] if len(args) > 1 else None)
    if cmd == "remove-server":
        # A switch is not a key: `remove-server --write` (key forgotten) would otherwise
        # take `--write` as the server name, report "nothing to do" and return 0.
        allowed = {"--write", "--include-active"}
        # SWALLOWING an unknown switch is the same trap as taking it for the key: `--writ`
        # (one letter short) would otherwise report a dry run with exit 0, and the operator
        # would believe the server is gone.
        unknown = [a for a in args[1:] if a.startswith("-") and a not in allowed]
        if unknown:
            print(f"ERROR: unknown option(s): {' '.join(unknown)}")
            return 2
        keys = [a for a in args[1:] if not a.startswith("-")]
        if not keys:
            print("ERROR: remove-server needs a server key.")
            return 2
        return _remove_server(keys[0], "--write" in args, "--include-active" in args)
    if cmd == "create-container":
        keys = [a for a in args[1:] if not a.startswith("-")]
        if not keys:
            print("ERROR: create-container needs a server key.")
            return 2
        return asyncio.run(_create_container(keys[0]))
    print(__doc__)
    print("Commands: stop-all [record-file] | start-recorded [record-file] | backfill-events <server>")
    print("          fix-links [--write]       set account links to the correct game (dry run without --write)")
    print("          smoke-token [account]     access token for acceptance checks (only the token on stdout)")
    print("          create-container <server> create the container of a new server for the FIRST time")
    print("          remove-server <server> [--write] [--include-active]")
    print("                                    remove a registry row + its telemetry (dry run without --write;")
    print("                                    --include-active = also an enabled server)")
    return 2


if __name__ == "__main__":  # pragma: no cover — entry point
    raise SystemExit(main())
