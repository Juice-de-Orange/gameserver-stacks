"""Reading/writing the PZ server.ini + mod lists + SandboxVars.

IMPORTANT: env-managed keys (Public/Password/RCON/PublicName) are overwritten by the image from
the container env on every boot -> blocked here, they must be changed through the env. Mods/
WorkshopItems are protected by SELF_MANAGED_MODS -> the dashboard manages them.
"""
from __future__ import annotations

import hashlib
import json
import os


ENV_MANAGED = {"Public", "PublicName", "Password", "RCONPassword"}

# Keys that belong to the mod workflow and must NOT be written through the config editor.
#
# Setting them past the curated editor bypasses preflight, mandatory backup, world quarantine
# and boot proof all at once, the very chain `services/mods/apply.py` exists to enforce.
# `PUT /{key}/config` depends on `can_edit_sandbox`, not on `can_manage_mods`: without this
# block a config maintainer could clear `Mods=` and `WorkshopItems=` and change `Map=`. The
# result is orphaned world cells without a map provider, and the join crash only shows up on
# the next restart.
MOD_MANAGED = {"Mods", "WorkshopItems", "Map"}


# The path helpers take the `game_servers` row: base = server.data_dir, slug from extra/key.
def _base_dir(server) -> str:
    return server.data_dir


def _basename(server) -> str:
    """File name stem of the server files (`<name>.ini`, `<name>_SandboxVars.lua`, …).

    The fallback to `server.key` is a SILENT trap: the name must match what the container
    receives as `SERVERNAME` (`drivers/pz.container_env`, same derivation). If
    `extra.servername` is missing, both fall back to `server.key`; they then match each other
    but no longer the EXISTING files: reading returns a 500, and a raw write creates a second
    `.ini` that the server never reads. Whoever sets `extra` when adopting an existing server
    and omits `servername` triggers exactly that.
    """
    # "servertest" is the game's own default server name.
    return json.loads(server.extra or "{}").get("servername") or server.key or "servertest"


def ini_path(server) -> str:
    return os.path.join(_base_dir(server), "data", "Server", f"{_basename(server)}.ini")


def sandbox_path(server) -> str:
    return os.path.join(_base_dir(server), "data", "Server", f"{_basename(server)}_SandboxVars.lua")


def spawnregions_path(server) -> str:
    """File that defines the selectable spawn regions on the dedicated server."""
    return os.path.join(_base_dir(server), "data", "Server", f"{_basename(server)}_spawnregions.lua")


def world_save_path(server) -> str:
    """World save folder (multiplayer). Delete it -> PZ generates a fresh world on the next start.
    Needed to switch map mods cleanly (a world is firmly bound to its maps)."""
    return os.path.join(_base_dir(server), "data", "Saves", "Multiplayer", _basename(server))


def read_raw(path: str) -> str:
    # errors="replace" for the DISPLAY/JSON raw-content endpoints: non-UTF-8 bytes → U+FFFD, so
    # JSON serialisation does not fail on lone surrogates. The lossless EDIT path uses
    # _read_preserving instead (see below).
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def file_revision(path: str) -> str:
    """Fingerprint of the file content, the basis of conflict detection in the raw editor.

    The raw editor sends back the WHOLE file. Without a check, anything written since loading
    (by the GUI tab of the same page, a second admin, `mods/apply` or a version change) would be
    silently lost. The client receives the fingerprint on read and sends it along on write; if
    it no longer matches, the router rejects the write.

    Over the BYTES, not the decoded text: `read_raw` replaces foreign bytes with U+FFFD, so two
    different files could yield the same string there. And not over the modification time: an
    `os.replace` without a content change (`set_ini_keys_at` rewrites the file even when the
    value stays the same) would otherwise turn every second save into a conflict.
    """
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _read_preserving(path: str) -> str:
    """Like read_raw, but with `surrogateescape`: non-UTF-8 bytes are read as lone surrogates and
    written back by write_raw as exactly the same bytes (lossless round trip). ONLY internal, for
    set_ini_keys_at: a single-key edit rewrites the WHOLE file; with `replace`, foreign bytes of
    UNRELATED values would permanently become U+FFFD."""
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_raw(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    # surrogateescape re-encodes the foreign bytes read by _read_preserving exactly;
    # a no-op for normal strings (router content, mods, sandbox).
    with open(tmp, "w", encoding="utf-8", newline="\n", errors="surrogateescape") as f:
        f.write(text)
    os.replace(tmp, path)


def get_ini_at(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in read_raw(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def set_ini_keys_at(path: str, updates: dict[str, str]) -> None:
    # Injection protection (sink guard): values are written verbatim as a `key=value` line.
    # A line break in the key/value would inject arbitrary ADDITIONAL lines, which would allow
    # setting blocked keys (env-managed, not whitelisted) past the router checks.
    # Reject hard instead of sanitising silently (visible error); routers translate to HTTP 400.
    for key, value in updates.items():
        if "\r" in f"{key}{value}" or "\n" in f"{key}{value}":
            raise ValueError(f"Line break not allowed in an INI key/value: {key!r}")
    done: set[str] = set()
    out: list[str] = []
    for line in _read_preserving(path).splitlines():  # lossless: foreign bytes of unrelated values survive
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                done.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in done:
            out.append(f"{key}={value}")
    write_raw(path, "\n".join(out) + "\n")


def get_mods(server) -> dict[str, list[str]]:
    """Mod list from the server's `.ini`."""
    data = get_ini_at(ini_path(server))
    workshop = [x for x in data.get("WorkshopItems", "").split(";") if x]
    mods = [x for x in data.get("Mods", "").split(";") if x]
    return {"workshop_items": workshop, "mods": mods}


# --- Secrets in the raw editor -----------------------------------------------------------------
#
# `GET /config/raw` would otherwise return the complete .ini in clear text, including
# `RCONPassword` and `Password`, and the config page loads this endpoint on EVERY visit. Masking
# only in `config_fields` would therefore not be enough.
#
# To keep the editor usable, the pair is deliberately reversible: on read exactly the secret
# lines are replaced by dots, on write exactly those lines are restored from the file on the
# server. Leaving the dots changes nothing; overwriting them changes nothing either, since the
# env-managed values come from the container env anyway and would be overwritten on the next
# boot.

MASK = "••••••"


def _is_secret(name: str) -> bool:
    """Does this key have to be masked?

    **Two reasons.** `ENV_MANAGED` covers the four keys the image writes from the container
    env; that is a statement about WHERE a value comes from, not about whether it IS a secret.
    `DiscordToken` is a standard key in every PZ `server.ini`, right next to
    `DiscordEnable`/`DiscordChannel`, and is neither env-managed nor mod-managed: without the
    second check it would be returned in clear text by `GET /config` and `GET /config/raw` and
    be writable on top. A Discord bot token is a control channel, not a join password.

    Vintage Story does the same (`looks_like_secret` for EVERY key of the file).
    """
    from .drivers.base import looks_like_secret

    return name in ENV_MANAGED or looks_like_secret(name)


def mask_env_managed(content: str) -> str:
    """Replace the values of secret and env-managed keys with dots (line by line, rest untouched)."""
    out: list[str] = []
    for line in content.splitlines(keepends=True):
        core = line.rstrip("\r\n")
        newline = line[len(core):]
        if "=" in core and not core.lstrip().startswith("#"):
            key, value = core.split("=", 1)
            if _is_secret(key.strip()) and value.strip():
                out.append(f"{key}={MASK}{newline}")
                continue
        out.append(line)
    return "".join(out)


def restore_env_managed(new: str, original: str) -> str:
    """Restore masked lines from the original before writing."""
    real = {
        k.strip(): v
        for k, v in (
            z.split("=", 1) for z in original.splitlines()
            if "=" in z and not z.lstrip().startswith("#")
        )
        if _is_secret(k.strip())
    }
    out: list[str] = []
    for line in new.splitlines(keepends=True):
        core = line.rstrip("\r\n")
        newline = line[len(core):]
        if "=" in core and not core.lstrip().startswith("#"):
            key, _ = core.split("=", 1)
            name = key.strip()
            if _is_secret(name) and name in real:
                out.append(f"{key}={real[name]}{newline}")
                continue
        out.append(line)
    return "".join(out)
