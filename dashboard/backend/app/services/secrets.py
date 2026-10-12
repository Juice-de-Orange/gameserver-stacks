"""Game-server secrets entered in the UI, stored encrypted (`server_secrets`).

**Slots.** `rcon` — the console password (RCON / BattlEye); `admin` — Project Zomboid's in-game
admin password (`ADMINPASSWORD`, falls back to the console password); `join` — the password
players type to join (Project Zomboid `PASSWORD`, Valheim `SERVER_PASS`).

**Where a secret comes from, in this order:**

1. the environment variable the row names — `rcon_password_env` for the console password,
   `extra.admin_password_env` / `extra.password_env` for the others. If it is set, it wins, so
   an installation that keeps its passwords in the `.env` behaves exactly as before;
2. the value stored here, entered in the UI;
3. nothing (`""`).

Older versions kept clear text in the row: the console password in `game_servers.rcon_password`,
the join password in `extra` (Project Zomboid `extra.password`, Valheim `extra.env.SERVER_PASS`).
`migrate_literals` moves them into this table at start; `split_literals` keeps new ones out of the
row on create and save. Until then `resolve` still reads them, so nothing breaks in between.

**Encryption.** Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`) with a key derived from
`GSS_SECRET_KEY` by HKDF-SHA256. Deriving instead of using the setting as the Fernet key means
any random string of at least 32 characters works (`openssl rand -hex 32`, like JWT_SECRET); the
`info` label keeps this key apart from anything else derived from the same value. The setting
is checked at start (`config.secret_key_problem`), so `encrypt` never runs without a usable key.

**Nothing here ever returns a secret to a caller outside the backend.** `status` says only
whether a value is set and where it comes from; the API routes build on that.
"""
from __future__ import annotations

import base64
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlmodel import Session, select

from ..config import secret_key_problem, settings
from ..models.player import utcnow_naive
from ..models.server import GameServer, GameType
from ..models.server_secret import ServerSecret

log = logging.getLogger("uvicorn.error")

RCON = "rcon"    # the console password (RCON / BattlEye)
ADMIN = "admin"  # Project Zomboid's in-game admin password
JOIN = "join"    # the join password: Project Zomboid PASSWORD, Valheim SERVER_PASS
SLOTS = (RCON, ADMIN, JOIN)
_INFO = b"stacks-console/server-secrets/v1"

# Where the NAME of a slot's environment variable is kept in `extra` (RCON: its own column).
_ENV_KEY = {ADMIN: "admin_password_env", JOIN: "password_env"}

# Clear text an older version kept in `extra`: game type → slot → path in `extra`.
LITERALS: dict[GameType, dict[str, tuple[str, ...]]] = {
    GameType.pz: {JOIN: ("password",)},
    GameType.valheim: {JOIN: ("env", "SERVER_PASS")},
}


class SecretKeyError(RuntimeError):
    """GSS_SECRET_KEY is missing or too short — nothing can be encrypted or decrypted."""


def _fernet(key: str | None = None) -> Fernet:
    raw = settings.secret_key if key is None else key
    if (problem := secret_key_problem(raw)):
        raise SecretKeyError(problem)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(raw.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str, *, key: str | None = None) -> str:
    return _fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str, *, key: str | None = None) -> str:
    """The clear text — or raises `InvalidToken` if the token was made with another key."""
    return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")


def _row(session: Session, server_id: int, slot: str) -> ServerSecret | None:
    return session.exec(
        select(ServerSecret).where(ServerSecret.server_id == server_id, ServerSecret.slot == slot)
    ).first()


def store(session: Session, server_id: int, value: str, slot: str = RCON) -> None:
    """Store (or replace) a secret. An empty value removes it. The caller commits."""
    row = _row(session, server_id, slot)
    if not value:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = ServerSecret(server_id=server_id, slot=slot)
    row.ciphertext = encrypt(value)
    row.updated_at = utcnow_naive()
    session.add(row)


def store_for(server: GameServer, value: str, slot: str) -> None:
    """`store` in a session of its own, committed — for the drivers' config writers, which get a
    detached row. The row is looked up by key when the copy carries no id."""
    from ..db import engine

    with Session(engine) as s:
        server_id = server.id
        if server_id is None:
            row = s.exec(select(GameServer).where(GameServer.key == server.key)).first()
            server_id = row.id if row is not None else None
        if server_id is None:
            raise ValueError(f"server '{server.key}' is not in the registry")
        store(s, server_id, value, slot)
        s.commit()


def stored(server_id: int | None, slot: str = RCON) -> str | None:
    """The stored clear text, `None` if nothing is stored.

    A row that cannot be decrypted (GSS_SECRET_KEY was changed) counts as not stored and is
    logged: the console then fails with a wrong password, which `status` explains.
    """
    if server_id is None:
        return None
    from ..db import engine

    try:
        with Session(engine) as s:
            row = _row(s, server_id, slot)
    except Exception:  # noqa: BLE001 — the env variable or "" still answer; logged, not hidden
        log.warning("secrets: the stored %s secret of server id %s cannot be read from the "
                    "database", slot, server_id, exc_info=True)
        return None
    if row is None:
        return None
    try:
        return decrypt(row.ciphertext)
    except (InvalidToken, SecretKeyError):
        log.warning("secrets: the stored %s secret of server id %s cannot be decrypted — was "
                    "GSS_SECRET_KEY changed? Enter the password again.", slot, server_id)
        return None


def _extra(server: GameServer) -> dict:
    try:
        extra = json.loads(server.extra or "{}")
    except ValueError:
        return {}
    return extra if isinstance(extra, dict) else {}


def env_name(server: GameServer, slot: str = RCON) -> str:
    """The NAME of the environment variable that holds this slot's secret, `""` if none."""
    if slot == RCON:
        return server.rcon_password_env or ""
    name = _extra(server).get(_ENV_KEY.get(slot, ""))
    return name if isinstance(name, str) else ""


def _literal(server: GameServer, slot: str) -> str | None:
    """Clear text an older version left in the row (`None`: there is none)."""
    if slot == RCON:
        return server.rcon_password or None
    path = LITERALS.get(GameType(server.game_type), {}).get(slot) if server.game_type else None
    node: object = _extra(server)
    for part in path or ():
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return str(node) if path and node not in (None, "") else None


def split_literals(game_type: GameType | str, extra: dict) -> dict[str, str]:
    """Take the clear-text secrets out of `extra` (in place); returns `{slot: value}`.

    For create and save: what the form or an API client sent goes into this table, never into
    the row. An empty value is returned too — it means "remove the stored one". One exception
    stays in the row: an EMPTY Valheim `SERVER_PASS`. To the image an empty variable means "no
    password", a missing one its default password `secret` (`ValheimDriver.container_env`), so
    the empty string is the statement and carries nothing secret.
    """
    found: dict[str, str] = {}
    for slot, path in LITERALS.get(GameType(game_type), {}).items():
        node = extra
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or path[-1] not in node:
            continue
        value = "" if node[path[-1]] is None else str(node[path[-1]])
        found[slot] = value
        if value or path != ("env", "SERVER_PASS"):
            node.pop(path[-1])
    return found


def store_found(session: Session, server_id: int, found: dict[str, str]) -> None:
    """`store` every value `split_literals` returned. The caller commits."""
    for slot, value in found.items():
        store(session, server_id, value, slot)


def resolve(server: GameServer, slot: str = RCON) -> str:
    """The secret the container and the console use: env variable → stored value → `""`."""
    name = env_name(server, slot)
    if name and (value := os.environ.get(name)):
        return value
    value = stored(server.id, slot)
    if value is not None:
        return value
    # Only a row an older version wrote and `migrate_literals` has not reached yet.
    return _literal(server, slot) or ""


def status(server: GameServer, slot: str = RCON, session: Session | None = None) -> dict:
    """Whether the secret is set and where it comes from — never the value.

    `source`: `env` (the named variable is set and wins), `stored` (entered in the UI),
    `none`. `unreadable` is True if a stored value exists but cannot be decrypted.
    """
    name = env_name(server, slot)
    env_set = bool(name and os.environ.get(name))
    has_row = False
    unreadable = False
    if server.id is not None:
        if session is not None:
            row = _row(session, server.id, slot)
        else:
            from ..db import engine

            with Session(engine) as s:
                row = _row(s, server.id, slot)
        if row is not None:  # a database error raises: "unknown" must not read as "not set"
            has_row = True
            try:
                decrypt(row.ciphertext)
            except (InvalidToken, SecretKeyError):
                unreadable = True
    stored_ok = has_row and not unreadable
    if env_set:
        source = "env"
    elif stored_ok or _literal(server, slot):
        source = "stored"
    else:
        source = "none"
    return {"source": source, "env_name": name, "env_set": env_set, "stored": stored_ok,
            "unreadable": unreadable}


def migrate_literals(engine) -> int:
    """Move clear-text secrets out of the rows into this table: the console password from
    `game_servers.rcon_password`, the join password from `extra` (`LITERALS`).

    Runs at every start; idempotent (an empty column, an `extra` without them is skipped). The
    clear text was readable for anybody with database access or a database dump; after this it
    is not. A value already stored for the slot is kept (it is the newer one). Returns the
    number of secrets moved.
    """
    moved = 0
    with Session(engine) as s:
        for srv in s.exec(select(GameServer).where(GameServer.rcon_password != "")).all():
            if srv.id is None:
                continue
            if _row(s, srv.id, RCON) is None:
                store(s, srv.id, srv.rcon_password)
            srv.rcon_password = ""
            s.add(srv)
            moved += 1
        for srv in s.exec(select(GameServer).where(GameServer.game_type.in_(list(LITERALS)))).all():
            extra = _extra(srv)
            found = split_literals(srv.game_type, extra)
            # Nothing to move: no literal, or only the empty Valheim SERVER_PASS that stays.
            if srv.id is None or extra == _extra(srv):
                continue
            for slot, value in found.items():
                if value and _row(s, srv.id, slot) is None:
                    store(s, srv.id, value, slot)
                    moved += 1
                # The old rule for Project Zomboid was "the dashboard's value wins over the
                # variable", so an emptied password beat `password_env`. Now the variable wins.
                if not value and env_name(srv, slot):
                    log.warning("secrets: %s had its %s password emptied in the dashboard, but %s is "
                                "named; from now on the variable wins if it is set", srv.key, slot,
                                env_name(srv, slot))
            srv.extra = json.dumps(extra)
            s.add(srv)
        s.commit()
    if moved:
        log.info("secrets: %d plain-text password(s) moved into encrypted storage", moved)
    return moved
