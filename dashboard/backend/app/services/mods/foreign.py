"""Mod management for Minecraft, Vintage Story, Factorio and Valheim: files instead of workshop IDs.

The same promise as for Project Zomboid: **editing changes nothing on the running server.**
Uploaded files are stored disabled at first; only "apply" moves them into place, restarts and
verifies the boot. On failure everything goes back.

The failure modes, however, are **different** from PZ. There are no map cells; what goes wrong
here:

- **wrong loader**: a Fabric mod in a Paper server's plugin folder is never loaded.
- **wrong game version**: a plugin for 1.20 on a 1.21 server aborts at start.
- **duplicate mod**: the same mod ID twice (e.g. two versions of the same jar).
- **broken archive**: not a readable jar/zip.

What CANNOT be determined beforehand: whether two formally compatible mods get in each other's
way at runtime. Only the boot proof with its rollback protects against that, not the
preflight, and the UI says exactly that.

Factorio and Valheim each bring a failure mode of their own that MC/VS do not have:

- **Factorio** declares dependencies in MACHINE-READABLE form (`info.json`, with the operators
  `>=`, `?`, `!`). It is the only one of the four where a mod set can really be *resolved*: a
  missing required dependency, a version conflict and a declared incompatibility are computed
  here, not guessed. In addition, Factorio lets nobody in whose mod set does not EXACTLY match
  the server's. A broken mod set there is not a crash but a door that stays shut.
- **Valheim** is the opposite case: a `.dll` carries its metadata in .NET attributes, which
  cannot be read without an assembly parser. So only what CAN be checked is checked: is it an
  assembly at all (MZ signature), is the loader switched on, is there a `manifest.json` next
  to it. Everything else is reported explicitly as **not verifiable** rather than as "OK".

Locations (verified on real servers or taken from the image documentation):
- Minecraft/Paper → `<data_dir>/data/plugins`, Fabric/Forge → `<data_dir>/data/mods`
- Vintage Story  → `<data_dir>/gamedata/vs/Mods` (NOT `gamedata/Mods`, see the VS driver)
- Factorio       → `<data_dir>/data/mods`
- Valheim        → `<data_dir>/config/bepinex/plugins`
Disabled files live next to them in `<folder>.disabled`: visible, but ignored by the server.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field

from ...models.server import GameType
from .preflight import BLOCKER, WARNING, Finding, PreflightResult

log = logging.getLogger("uvicorn.error")

DISABLED_SUFFIX = ".disabled"
# What the respective server loads at all.
EXTENSIONS = {
    GameType.minecraft: (".jar",),
    GameType.vintagestory: (".zip", ".dll", ".cs"),
    GameType.factorio: (".zip",),
    GameType.valheim: (".dll",),
}

# Games that also load a mod as an UNPACKED FOLDER. For Minecraft/Paper that would be wrong:
# Paper creates a CONFIGURATION folder of the same name next to every jar, and counting those
# as mods would double the list and produce a loader blocker per folder.
FOLDERS_ARE_MODS = frozenset({GameType.vintagestory, GameType.factorio, GameType.valheim})


def _vs_json(text: str):
    """Read `modinfo.json` as tolerantly as Vintage Story itself (Newtonsoft `JsonTextReader`).

    The game accepts a comma before `}`/`]`, `//` and `/* */` comments and raw line breaks
    inside strings. Strict `json.loads` does not, and would block widely used mods that every
    client loads as `MODFILE_UNREADABLE`; an active mod with a blocker then blocks every start
    via `guard_boot`. Single quotes and unquoted keys remain an error: those would need a
    parser of their own. Runs in a single pass (the file comes from an upload).
    """
    out: list[str] = []
    n = len(text)
    i = 0
    in_str = escaped = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return json.loads("".join(out), strict=False)


def _vs_meta(data: dict) -> dict:
    """Identity from a Vintage Story `modinfo.json`.

    `game_versions` is the required GAME version (`dependencies.game`, a minimum version
    according to VS), not the names of the other dependencies. The version check only takes
    effect for Vintage Story when the server has a pinned version to compare against.

    A file whose root or `dependencies` is not an object is rejected by the game as well. It is
    reported as a `ValueError` (→ `MODFILE_UNREADABLE` for this one mod) instead of crashing
    the whole mod page with an `AttributeError`.
    """
    if not isinstance(data, dict):
        raise ValueError("modinfo.json is not an object")
    low = {str(k).lower(): v for k, v in data.items()}
    deps_raw = low.get("dependencies") or {}
    if not isinstance(deps_raw, dict):
        raise ValueError("dependencies is not an object")
    deps = {str(k).lower(): v for k, v in deps_raw.items()}
    game = str(deps.get("game") or "").strip()
    return {
        "mod_id": str(low.get("modid") or ""),
        "name": str(low.get("name") or ""),
        "version": str(low.get("version") or ""),
        "loader": "vintagestory",
        "game_versions": (game,) if game and game != "*" else (),
    }


@dataclass
class ForeignMod:
    filename: str
    enabled: bool
    size: int = 0
    mod_id: str = ""
    name: str = ""
    version: str = ""
    loader: str = ""          # "paper" | "fabric" | "forge" | "vintagestory" | "factorio" | "bepinex" | ""
    game_versions: tuple[str, ...] = ()
    problems: tuple[str, ...] = field(default_factory=tuple)
    # Raw dependency declarations, as the game writes them. Only Factorio fills this with
    # something resolvable ("base >= 2.0", "? optional", "! incompatible"); Valheim provides at
    # most the Thunderstore form "Author-Package-1.2.3".
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    # What could NOT be determined about this file. Empty means "everything read"; filled means
    # "fine as far as visible", which is not the same as "checked".
    unverifiable: str = ""


# --------------------------------------------------------------------- locations

def mods_dir(server) -> str:
    """Where the server actually looks."""
    if server.game_type == GameType.factorio:
        return os.path.join(server.data_dir, "data", "mods")
    if server.game_type == GameType.valheim:
        # BepInEx loads from `plugins`; the folder is in the image's `/config` volume.
        return os.path.join(server.data_dir, "config", "bepinex", "plugins")
    if server.game_type == GameType.vintagestory:
        from ..drivers.vintagestory import VintageStoryDriver

        return VintageStoryDriver.mods_dir(server)
    # Minecraft: Paper loads plugins, Fabric/Forge load mods; two different folders.
    sub = "mods" if (server.server_type or "").upper() in {"FABRIC", "FORGE", "NEOFORGE"} else "plugins"
    return os.path.join(server.data_dir, "data", sub)


def disabled_dir(server) -> str:
    return mods_dir(server) + DISABLED_SUFFIX


# Where removed mod files go. **Moved, not deleted**, the same line as the quarantine in
# `apply.py`: what is gone cannot be recovered after a mistake, and a mod file is sometimes the
# only copy of a version that is no longer available online.
REMOVED_SUFFIX = ".removed"


def removed_dir(server) -> str:
    return mods_dir(server) + REMOVED_SUFFIX


def remove_file(server, filename: str, *, active: set[str]) -> dict:
    """Take a mod file out of service by moving it, not by deleting it.

    Without removal `mods.disabled` grows without bound, and for Factorio every file carries a
    version: a folder full of old versions is not just ballast there but a source of errors
    when resolving dependencies.

    **An ACTIVE file is not touched.** It would be gone the next moment while the running
    server still has it loaded, and with Factorio no client gets in afterwards. To get rid of
    it, disable it first and apply; then the preflight, the mandatory backup and the boot proof
    apply.
    """
    check_filename(server, filename)
    if filename in active:
        raise ValueError(
            f"“{filename}” is active. Disable it and apply first — then the preflight, the "
            f"mandatory backup and the boot proof apply. After that it can be removed."
        )

    source = ""
    for folder in (mods_dir(server), disabled_dir(server)):
        candidate = os.path.join(folder, filename)
        if os.path.isfile(candidate):
            source = candidate
            break
    if not source:
        raise FileNotFoundError(f"“{filename}” is neither in the mod folder nor in the disabled folder.")

    target_folder = removed_dir(server)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(target_folder, f"{stamp}-{filename}")
    try:
        os.makedirs(target_folder, exist_ok=True)
        shutil.move(source, target)
    except OSError as e:
        raise ValueError(f"“{filename}” could not be set aside: {e}") from e
    return {"file": filename, "set_aside": target, "from": os.path.dirname(source)}


def list_removed(server) -> list[dict]:
    """What has been set aside, so that "not deleted" is also visible."""
    folder = removed_dir(server)
    out: list[dict] = []
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except FileNotFoundError:
        return []
    except OSError as e:
        # Unreadable is not empty: otherwise the UI would claim "nothing here" about a folder
        # it could not look into.
        raise ValueError(f"The folder of removed mods cannot be read ({folder}): {e}") from e
    for name in names:
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
            when = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc).isoformat()
        except OSError:
            continue
        out.append({"name": name, "size": size, "removed_at": when})
    return out


def purge_removed(server, *, days: int) -> int:
    """Really delete what was set aside once the retention period has passed."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    folder = removed_dir(server)
    removed_count = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc) >= cutoff:
                continue
            os.remove(path)
            removed_count += 1
        except OSError:
            continue
    return removed_count


def supported(server) -> bool:
    return server.game_type in EXTENSIONS


def expected_loader(server) -> str:
    if server.game_type == GameType.factorio:
        return "factorio"
    if server.game_type == GameType.valheim:
        return "bepinex"
    if server.game_type == GameType.vintagestory:
        return "vintagestory"
    t = (server.server_type or "").upper()
    if t in {"FABRIC"}:
        return "fabric"
    if t in {"FORGE", "NEOFORGE"}:
        return "forge"
    return "paper"   # Paper/Spigot/Bukkit load plugins with plugin.yml


# --------------------------------------------------------------------- reading metadata

def _yaml_first(text: str, key: str) -> str:
    """Tiny YAML grab for `plugin.yml`, where the values are flat `key: value` lines.

    Deliberately no YAML parser as a new dependency: only three flat fields are needed.
    """
    m = re.search(rf"^{key}\s*:\s*(.+?)\s*$", text, re.M | re.I)
    if not m:
        return ""
    return m.group(1).strip().strip("'\"")


def read_metadata(path: str) -> dict:
    """Read mod identity, version, loader and target versions from the archive.

    Unreadable → `{"problem": ...}`; the caller turns that into a blocker instead of silently
    waving the file through as "OK".
    """
    # Valheim: a BepInEx mod is a .NET assembly, not an archive. Its metadata sits in .NET
    # attributes, which cannot be read without an assembly parser, so the only check is whether
    # it is an assembly AT ALL. The MZ signature costs two bytes and catches the most common
    # real error: a renamed text file or a partial download.
    if path.lower().endswith(".dll"):
        try:
            with open(path, "rb") as f:
                signature = f.read(2)
        except OSError as e:
            return {"problem": f"file cannot be read: {e}"}
        if signature != b"MZ":
            return {"problem": "not a valid .NET assembly (MZ signature missing)"}
        return {
            "mod_id": os.path.splitext(os.path.basename(path))[0],
            "name": os.path.splitext(os.path.basename(path))[0],
            "version": "", "loader": "bepinex", "game_versions": (),
            # Explicit, not silent: what is missing here is not a verified all-clear.
            "unverifiable": "version and dependencies are stored in .NET attributes and "
                              "cannot be read without an assembly parser.",
        }

    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())

            # Factorio: `info.json` is INSIDE the mod folder within the zip
            # (`Krastorio2_1.3.24/info.json`), not at the top level.
            hits = [n for n in names if n == "info.json" or n.endswith("/info.json")]
            if hits:
                data = json.loads(z.read(sorted(hits, key=len)[0]).decode("utf-8", errors="replace"))
                return _factorio_meta(data)

            # Valheim/Thunderstore as a zip: `manifest.json` names name, version and dependencies.
            if "manifest.json" in names:
                data = json.loads(z.read("manifest.json").decode("utf-8-sig", errors="replace"))
                return _thunderstore_meta(data)

            # Vintage Story
            if "modinfo.json" in names:
                return _vs_meta(_vs_json(z.read("modinfo.json").decode("utf-8-sig", errors="replace")))

            # Fabric
            if "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json").decode("utf-8", errors="replace"))
                depends = data.get("depends") or {}
                return {
                    "mod_id": str(data.get("id") or ""),
                    "name": str(data.get("name") or ""),
                    "version": str(data.get("version") or ""),
                    "loader": "fabric",
                    "game_versions": (str(depends.get("minecraft") or ""),) if depends.get("minecraft") else (),
                }

            # Forge / NeoForge
            for meta in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if meta in names:
                    text = z.read(meta).decode("utf-8", errors="replace")
                    return {
                        "mod_id": _toml_value(text, "modId"),
                        "name": _toml_value(text, "displayName"),
                        "version": _toml_value(text, "version"),
                        "loader": "forge",
                        "game_versions": (),
                    }

            # Paper / Spigot / Bukkit
            for meta in ("paper-plugin.yml", "plugin.yml"):
                if meta in names:
                    text = z.read(meta).decode("utf-8", errors="replace")
                    return {
                        "mod_id": _yaml_first(text, "name"),
                        "name": _yaml_first(text, "name"),
                        "version": _yaml_first(text, "version"),
                        "loader": "paper",
                        "game_versions": (_yaml_first(text, "api-version"),) if _yaml_first(text, "api-version") else (),
                    }
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as e:
        return {"problem": f"archive cannot be read: {e}"}
    return {"problem": "no known mod description found in the archive"}


def _factorio_meta(data: dict) -> dict:
    """`info.json` → metadata. The dependencies stay RAW so that `check` can resolve them."""
    return {
        "mod_id": str(data.get("name") or ""),
        "name": str(data.get("title") or data.get("name") or ""),
        "version": str(data.get("version") or ""),
        "loader": "factorio",
        # `factorio_version` is the MINIMUM version in the sense of "built for this series",
        # e.g. "2.0". It goes into `game_versions`, where `_needs_newer_server` evaluates it.
        "game_versions": (str(data.get("factorio_version") or ""),) if data.get("factorio_version") else (),
        "dependencies": tuple(str(d) for d in (data.get("dependencies") or []) if str(d).strip()),
    }


def _thunderstore_meta(data: dict) -> dict:
    """Thunderstore `manifest.json` → metadata (Valheim, when stored as a package zip)."""
    return {
        "mod_id": str(data.get("name") or ""),
        "name": str(data.get("name") or ""),
        "version": str(data.get("version_number") or ""),
        "loader": "bepinex",
        "game_versions": (),
        "dependencies": tuple(str(d) for d in (data.get("dependencies") or []) if str(d).strip()),
    }


def _toml_value(text: str, key: str) -> str:
    m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else ""


def _folder_metadata(path: str) -> dict:
    """Unpacked mod (folder): description from the file the respective game expects there.

    Vintage Story also loads mods as folders (an RCON mod is typically installed that way).
    So does Factorio (`<Name>_<Version>/info.json`), and a Thunderstore package is unpacked as
    a folder with `manifest.json`.
    """
    fac = os.path.join(path, "info.json")
    if os.path.isfile(fac):
        try:
            with open(fac, encoding="utf-8", errors="replace") as f:
                return _factorio_meta(json.load(f))
        except (OSError, ValueError) as e:
            # As with `modinfo.json` below: a BROKEN info.json is different from none at all.
            return {"mod_id": os.path.basename(path), "name": os.path.basename(path),
                    "version": "", "loader": "factorio", "game_versions": (),
                    "problem": f"info.json cannot be read ({e})"}

    manifest = os.path.join(path, "manifest.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8-sig", errors="replace") as f:
                return _thunderstore_meta(json.load(f))
        except (OSError, ValueError) as e:
            return {"mod_id": os.path.basename(path), "name": os.path.basename(path),
                    "version": "", "loader": "bepinex", "game_versions": (),
                    "problem": f"manifest.json cannot be read ({e})"}

    info = os.path.join(path, "modinfo.json")
    if os.path.isfile(info):
        try:
            with open(info, encoding="utf-8-sig", errors="replace") as f:
                return _vs_meta(_vs_json(f.read()))
        except (OSError, ValueError) as e:
            # **A BROKEN `modinfo.json` is different from none at all.** Silently falling back
            # to the folder name would contradict the rule `read_metadata` sets above, with two
            # consequences: no `MODFILE_UNREADABLE`, and the `mod_id` would become the folder
            # name, so `MOD_DUPLICATE` would no longer catch it either.
            return {"mod_id": os.path.basename(path), "name": os.path.basename(path),
                    "version": "", "loader": "vintagestory", "game_versions": (),
                    "problem": f"modinfo.json cannot be read ({e})"}
    # Without a description, the folder name is the most reliable identity there is.
    #
    # **`loader` stays EMPTY here.** `scan` calls this function for EVERY subfolder of every
    # game in `FOLDERS_ARE_MODS`. Claiming a loader (e.g. "vintagestory") just because
    # `info.json`, `manifest.json` and `modinfo.json` are missing would be a statement without
    # evidence, and `check` would turn it into a false `MOD_WRONG_LOADER` BLOCKER ("the server
    # would never load the file"), although BepInEx, for one, loads `plugins/` recursively.
    #
    # Empty means "unknown": `check` tests `if m.loader and …` and then stays silent. So that
    # this does not become a silent pass, a `unverifiable` note is added: a WARNING, like the
    # one `read_metadata` sets for `.dll`, not a `problem` (which would be a blocker).
    return {"mod_id": os.path.basename(path), "name": os.path.basename(path),
            "version": "", "loader": "", "game_versions": (),
            "unverifiable": "no info.json, manifest.json or modinfo.json — the format of "
                              "this folder cannot be determined"}


def _size_of(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------- inventory

def scan(server) -> list[ForeignMod]:
    """All mod files, active and disabled, with the metadata read from them."""
    exts = EXTENSIONS.get(server.game_type, ())
    out: list[ForeignMod] = []
    for folder, enabled in ((mods_dir(server), True), (disabled_dir(server), False)):
        try:
            names = sorted(os.listdir(folder))
        except FileNotFoundError:
            # Not present is different from not readable: `disabled/` is only created on the
            # first disable, and a fresh server has no mod folders at all yet.
            continue
        except OSError as e:
            # **Raise, so it cannot be overlooked.** A silently shorter list would carry no
            # marker: `check()` only iterates over this list, would find no blockers on an empty
            # one, and the apply would run stop, mandatory backup and restart, move nothing and
            # report success. The PZ counterpart `mods/inventory.py` turns this into a
            # fail-closed `WORKSHOP_DIR_UNREADABLE` blocker; here an exception is the sharper
            # tool, because `scan` has many callers and a flag could be overlooked by any of
            # them.
            raise OSError(
                f"The mod folder {folder} exists but cannot be listed ({e}). A shorter list "
                f"would be a false statement here — nothing is applied."
            ) from e
        for name in names:
            path = os.path.join(folder, name)
            if name.startswith("."):
                continue
            # Vintage Story also allows mods as an UNPACKED FOLDER (e.g. `VintageRCon/` with a
            # .dll inside). Looking only at files would make exactly such a mod invisible.
            #
            # NOT for Minecraft: Paper creates a CONFIGURATION folder of the same name next to
            # every jar (`Chunky/` next to `Chunky-Bukkit-1.4.40.jar`). Counting those as mods
            # would double the list and produce a loader blocker for every folder.
            folders_are_mods = server.game_type in FOLDERS_ARE_MODS
            is_dir = os.path.isdir(path)
            if is_dir and not folders_are_mods:
                continue
            if not is_dir and not os.path.isfile(path):
                continue
            if not is_dir and exts and not name.lower().endswith(exts):
                continue
            meta = read_metadata(path) if not is_dir else _folder_metadata(path)
            out.append(ForeignMod(
                filename=name,
                enabled=enabled,
                size=_size_of(path),
                mod_id=meta.get("mod_id", ""),
                name=meta.get("name", ""),
                version=meta.get("version", ""),
                loader=meta.get("loader", ""),
                game_versions=tuple(v for v in meta.get("game_versions", ()) if v),
                problems=_problems(meta),
                dependencies=tuple(meta.get("dependencies", ())),
                unverifiable=str(meta.get("unverifiable", "")),
            ))
    return out


def _problems(meta: dict) -> tuple[str, ...]:
    """`problem` is a blocker, `unverifiable` a warning; neither may disappear.

    The difference matters: "not readable" means the file is broken; "not verifiable" means it
    is fine but we know nothing about it. Treating both alike would be scaremongering in one
    direction and a silent "it's fine" in the other.
    """
    return tuple(meta[k] for k in ("problem",) if meta.get(k))


# --------------------------------------------------------------------- check catalogue

async def check(server, enabled: set[str], mods: list[ForeignMod] | None = None) -> PreflightResult:
    """Check the selection that is about to be activated."""
    mods = mods if mods is not None else scan(server)
    want = expected_loader(server)
    findings: list[Finding] = []

    # **Fail-closed at the check choke point.** An enabled file the scan does not know is simply
    # not in `mods`, and thus invisible: with an empty list `check()` would find no blockers and
    # the apply would run stop, mandatory backup and restart, move nothing and report success.
    # Typical trigger: after switching PAPER→FABRIC `mods_dir` points at a different path, and
    # the stored selection names files that do not exist there. The PZ counterpart is
    # `ITEM_NOT_DOWNLOADED`.
    known = {m.filename for m in mods}
    for missing in sorted(enabled - known):
        findings.append(Finding(
            code="MODFILE_MISSING", level=BLOCKER, subject=missing,
            message=f"“{missing}” is selected but is not in the mod folder. "
                    f"The server would never load it.",
            fix="remove_file",
        ))

    by_id: dict[str, list[str]] = {}
    for m in mods:
        if m.filename not in enabled:
            continue

        if m.problems:
            findings.append(Finding(
                code="MODFILE_UNREADABLE", level=BLOCKER, subject=m.filename,
                message=f"“{m.filename}” cannot be read — {m.problems[0]}",
                fix="remove_file",
            ))
            continue

        if m.loader and m.loader != want:
            findings.append(Finding(
                code="MOD_WRONG_LOADER", level=BLOCKER, subject=m.filename,
                message=f"“{m.filename}” is for {m.loader}, this server is {want}. "
                        f"The server would never load the file.",
                fix="remove_file",
            ))

        if m.mod_id:
            by_id.setdefault(m.mod_id.lower(), []).append(m.filename)

        # **The running version comes from the version catalogue, not from one field.** For
        # Factorio `version_source = image_tag`: the truth is in `server.image_tag`, and
        # `game_version` stays empty on a newly created server. Reading only `game_version`
        # would switch `MOD_VERSION_MISMATCH` off for Factorio entirely.
        #
        # This is not a marginal check: `_factorio_dependencies` skips `base`, `space-age`,
        # `quality` and `elevated-rails` on the grounds that a version requirement on them is
        # checked here. Without it a mod for a newer game version would pass `check` AND
        # `apply` without a single finding, and only the boot proof would fail, after stop,
        # backup and move. The preflight exists to save exactly that cycle.
        from ..version_catalog import current_ref

        target = (current_ref(server) or "").strip()
        if target and m.game_versions and _needs_newer_server(target, m.game_versions, m.loader):
            findings.append(Finding(
                code="MOD_VERSION_MISMATCH", level=WARNING, subject=m.filename,
                message=f"“{m.filename}” requires at least {', '.join(m.game_versions)}, "
                        f"but the server runs {target}.",
            ))

    for mod_id, files in by_id.items():
        if len(files) > 1:
            findings.append(Finding(
                code="MOD_DUPLICATE", level=BLOCKER, subject=mod_id,
                message=f"The mod “{mod_id}” is active twice ({', '.join(sorted(files))}) — "
                        f"one of the files has to go.",
                detail=", ".join(sorted(files)),
                fix="remove_file",
            ))

    active = [m for m in mods if m.filename in enabled and not m.problems]
    if server.game_type == GameType.factorio:
        findings += _factorio_dependencies(active)
    if server.game_type == GameType.valheim:
        findings += await _valheim_checks(server, active)

    # What could NOT be determined surfaces as a warning, not as silence. The same rule as for
    # the workshop check: "not verifiable" is not "OK", and hiding it would pass off a gap as
    # an approval.
    for m in active:
        if m.unverifiable:
            findings.append(Finding(
                code="MOD_UNVERIFIABLE", level=WARNING, subject=m.filename,
                message=f"“{m.filename}” could only be checked in part — {m.unverifiable}",
                detail="The boot proof after applying is the real safeguard here.",
            ))

    return PreflightResult(findings)


# --------------------------------------------------------------------- Factorio: dependencies

# `info.json` declares dependencies as strings with a prefix:
#   "base >= 2.0"      required
#   "? optional-mod"   optional (missing is fine)
#   "(?) hidden"       optional, not shown in the game
#   "! incompatible"   must NOT be loaded at the same time
#   "~ mod"            required, but without influence on the load order
_FAC_DEP_RE = re.compile(
    r"^\s*(?P<prefix>!|\?|\(\?\)|~)?\s*(?P<name>[A-Za-z0-9_\-]+)"
    r"(?:\s*(?P<op>>=|<=|==|>|<|=)\s*(?P<ver>[\d.]+))?\s*$"
)

# Mods that belong to the game and do not exist as files. A dependency on them is always
# satisfied; without this list `base` would be a blocker in every single mod set.
FACTORIO_BUILTIN = frozenset({"base", "elevated-rails", "quality", "space-age"})


def _fac_ver(text: str) -> tuple[int, ...]:
    parts = []
    for piece in (text or "").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def _fac_satisfied(op: str, actual: tuple[int, ...], required: tuple[int, ...]) -> bool:
    """Compare versions only as deep as both sides go ("1.3" vs "1.3.24" → equal)."""
    depth = min(len(actual), len(required)) or 1
    a, b = actual[:depth], required[:depth]
    if op in (">=",):
        return a >= b
    if op == ">":
        return a > b
    if op in ("<=",):
        return a <= b
    if op == "<":
        return a < b
    return a == b          # "=" and "=="


def _factorio_dependencies(active: list[ForeignMod]) -> list[Finding]:
    """Really resolve the enabled mod set; the only case where that is possible.

    Why this weighs more here than for the other games: Factorio lets no client in whose mod
    set does not EXACTLY match the server's. An incomplete set is therefore not a crash visible
    in the log but a door that stays shut, with a message for the player and none on the
    server.
    """
    findings: list[Finding] = []
    have: dict[str, tuple[int, ...]] = {
        m.mod_id.lower(): _fac_ver(m.version) for m in active if m.mod_id
    }
    for m in active:
        for raw in m.dependencies:
            hits = _FAC_DEP_RE.match(raw)
            if not hits:
                findings.append(Finding(
                    code="FAC_DEP_UNREADABLE", level=WARNING, subject=m.filename,
                    message=f"The dependency “{raw}” of “{m.filename}” is in no known "
                            f"notation — it was not checked.",
                ))
                continue
            prefix = hits.group("prefix") or ""
            name = hits.group("name")
            op, ver = hits.group("op"), hits.group("ver")
            lower = name.lower()

            if prefix == "!":
                if lower in have:
                    findings.append(Finding(
                        code="FAC_INCOMPATIBLE", level=BLOCKER, subject=m.filename,
                        message=f"“{m.filename}” declares itself incompatible with “{name}” — "
                                f"both are active.",
                        detail=raw, fix="remove_file",
                    ))
                continue

            if lower in FACTORIO_BUILTIN:
                # `base` and the DLC mods are always there; only a version requirement on them
                # can fail, and `MOD_VERSION_MISMATCH` checks that via `factorio_version`.
                continue

            if lower not in have:
                if prefix in ("?", "(?)"):
                    continue      # optional: missing is explicitly allowed
                findings.append(Finding(
                    code="FAC_DEP_MISSING", level=BLOCKER, subject=m.filename,
                    message=f"“{m.filename}” needs “{name}”, but that mod is not active. "
                            f"The server would start — but nobody could join.",
                    detail=raw,
                ))
                continue

            if op and ver:
                actual = have[lower]
                required = _fac_ver(ver)
                if actual and required and not _fac_satisfied(op, actual, required):
                    findings.append(Finding(
                        code="FAC_DEP_VERSION", level=BLOCKER, subject=m.filename,
                        message=f"“{m.filename}” requires {name} {op} {ver}, but the active "
                                f"version is {'.'.join(str(t) for t in actual)}.",
                        detail=raw,
                    ))
    return findings


# --------------------------------------------------------------------- Valheim: the loader matters

async def _valheim_checks(server, active: list[ForeignMod]) -> list[Finding]:
    """For Valheim it is not the file that decides, but a switch.

    Plugins under `bepinex/plugins` do **nothing at all** without `BEPINEX=true`: the server
    starts, runs, and not a single mod is loaded. That is the most misleading state this game
    has: everything looks right and nothing takes effect.

    **The RUNNING container is asked, not the registry.** `list_mods()['loader_enabled']` reads
    `extra.env`, i.e. the desired state. The container only gets its env when it is created
    (`config_reload = recreate`); a `set_config` changes nothing on the running one, so the two
    sides can be arbitrarily far apart.

    If the container cannot be queried, that is a WARNING, neither a free pass nor a blocker:
    the same rule as for the workshop check, "not verifiable" is not "OK".
    """
    if not active:
        return []
    from ..drivers.valheim import ValheimDriver

    running = await ValheimDriver().loader_running(server)
    if running is False:
        return [Finding(
            code="VH_LOADER_OFF", level=BLOCKER, subject="BEPINEX",
            message=f"{len(active)} plugin(s) are enabled, but the running container has "
                    f"BEPINEX switched off — it would not load a single mod.",
            detail="Setting “Load BepInEx” in the configuration; it only takes effect after the "
                   "container has been rebuilt (Version tab).",
        )]
    if running is None:
        wanted = bool(ValheimDriver().list_mods(server).get("loader_enabled"))
        return [Finding(
            code="VH_LOADER_UNKNOWN", level=WARNING, subject="BEPINEX",
            # Two whole sentences instead of one with a built-in word: each is a fixed text
            # the frontend can translate.
            message=("Whether the running container loads BepInEx could not be determined — "
                     "the stored setting says on." if wanted else
                     "Whether the running container loads BepInEx could not be determined — "
                     "the stored setting says off."),
            detail="The container is not responding right now (or was created without this "
                   "variable). The boot proof after applying is the safeguard here.",
        )]
    return []


def _needs_newer_server(target: str, declared: tuple[str, ...], loader: str) -> bool:
    """Does the mod require a NEWER game version than the server has?

    Important, and corrected against real data: Paper/Spigot declare the **minimum** API under
    `api-version` in `plugin.yml`, not the target version. Most real plugins name an older
    number (1.13, 1.17) and run flawlessly on 1.21.x; a "does not match" warning would be wrong
    in almost every case and would bury the real findings.

    So a warning is raised only in the opposite, actually risky case: the mod requires more
    than the server offers. Incomparable declarations (`>=1.21`, `1.21.x`, `*`) trigger nothing.
    """
    from ..version_manager import parse_ver

    have = parse_ver(target)
    if not have:
        return False
    for d in declared:
        want = parse_ver(d)
        if not want:
            continue
        # Compare only as deep as both sides go ("1.21" vs "1.21.10" → equal).
        depth = min(len(want), len(have))
        if want[:depth] > have[:depth]:
            return True
    return False


# --------------------------------------------------------------------- apply

def _move(src: str, dst_dir: str, name: str) -> bool:
    """Move a mod file, and **never silently replace a file of the same name at the target**.

    `store_upload_stream` stores every uploaded file under its original name in the DISABLED
    folder and does not check whether a file of that name already exists in the active folder.
    For Valheim that is the normal case: BepInEx plugins usually carry no version in the file
    name. If the old version is then deselected, a plain `shutil.move` would push it over the
    freshly uploaded one (`os.rename` within one file system replaces silently), and the new
    version would be gone for good: no quarantine, no backup, no way back. The obvious way out
    is the very trigger: selecting the new version makes the preflight block with "duplicate
    active", and deselecting the old one is the button one presses next.

    Instead of replacing, the file found at the target is set aside (`.displaced-<n>`). It is
    no longer a mod afterwards (the extension no longer matches, `scan` does not see it), but
    it is still there.
    """
    target = os.path.join(dst_dir, name)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.exists(target):
            set_aside = target + ".displaced"
            n = 1
            while os.path.exists(set_aside):
                set_aside = f"{target}.displaced-{n}"
                n += 1
            os.replace(target, set_aside)
            log.warning(
                "mods(files): %s was already in %s — the version found there now sits next to "
                "it as %s and is NOT loaded. Whoever needs it renames it back on the host.",
                name, dst_dir, os.path.basename(set_aside),
            )
        shutil.move(src, target)
        return True
    except OSError as e:
        log.warning("mods(files): %s could not be moved: %s", name, e)
        return False


def set_enabled(server, enabled: set[str]) -> dict:
    """Move files between the active and the disabled folder.

    Returns what **really** changed, and what did not: `failed` names the files whose move
    failed. The caller must compare against it instead of reporting the requested selection
    as a success; otherwise a crashing file would stay in `mods/` unnoticed.
    """
    active, off = mods_dir(server), disabled_dir(server)
    turned_on, turned_off, failed = [], [], []
    mods = scan(server)
    for m in mods:
        if m.enabled and m.filename not in enabled:
            target = turned_off if _move(os.path.join(active, m.filename), off, m.filename) else failed
            target.append(m.filename)
        elif not m.enabled and m.filename in enabled:
            target = turned_on if _move(os.path.join(off, m.filename), active, m.filename) else failed
            target.append(m.filename)

    # **For Factorio it is not the folder that decides, but `mod-list.json`.** This module
    # derives `enabled` solely from whether the file is in the active folder, but Factorio
    # itself reads the on/off state from `mods/mod-list.json`. If nobody wrote that file, a zip
    # in the active folder with `"enabled": false` would count as active for the preflight, the
    # apply would report "N enabled", the boot proof would pass (the server does start), and the
    # mod would never be loaded. Clients with the mod would hang on join, because Factorio
    # compares the mod set exactly.
    if server.game_type == GameType.factorio:
        try:
            _factorio_write_modlist(server, {m.filename for m in mods}, enabled)
        except OSError as e:
            # Loud, not silent: without this file the move is only half done, and the caller
            # would otherwise report success for something the server does not see.
            failed.append(f"mod-list.json ({e})")
    return {"enabled": turned_on, "disabled": turned_off, "failed": failed}


def _factorio_write_modlist(server, all_files: set[str], enabled: set[str]) -> None:
    """Align `mods/mod-list.json` with the folder state; it is the file Factorio really reads.

    Built-in mods (`base`, `space-age`, `quality`, `elevated-rails`) do not exist as files and
    are left untouched: removing them from the list would interfere with the game itself.
    Entries for which there is no file (yet) also stay: they may come from a modpack, and
    deleting them is a decision this module does not make.
    """
    from ..drivers.factorio import _BUILTIN

    path = os.path.join(mods_dir(server), "mod-list.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = list(data.get("mods") or [])
    except (OSError, ValueError):
        entries = []

    # File name → mod name as Factorio knows it: `Krastorio2_1.3.24.zip` → `Krastorio2`.
    def _mod_name(filename: str) -> str:
        stem = filename[:-4] if filename.lower().endswith(".zip") else filename
        return stem.rsplit("_", 1)[0] if "_" in stem else stem

    required = {_mod_name(d): (d in enabled) for d in all_files}
    known = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "")
        known.add(name)
        if name in _BUILTIN or name not in required:
            continue
        e["enabled"] = required[name]
    for name, on in sorted(required.items()):
        if name not in known:
            entries.append({"name": name, "enabled": on})

    tmp = path + ".incoming"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mods": entries}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)   # atomic: half a mod-list.json keeps the server from starting


def check_filename(server, filename: str) -> str:
    """Check the target name BEFORE a single byte is read. Returns the safe base name."""
    safe = os.path.basename(filename).strip()
    exts = EXTENSIONS.get(server.game_type, ())
    if not safe or safe.startswith(".") or (exts and not safe.lower().endswith(exts)):
        raise ValueError(f"Only {', '.join(exts)} allowed — “{filename}” does not match.")
    return safe


def store_upload_stream(server, filename: str, chunks, max_bytes: int) -> ForeignMod:
    """Store an uploaded file **disabled**, block by block.

    `chunks` is an iterator over byte blocks. The size limit is checked WHILE writing, not
    afterwards, so an oversized upload never ends up in the backend's memory.
    """
    safe = check_filename(server, filename)
    off = disabled_dir(server)
    os.makedirs(off, exist_ok=True)
    path = os.path.join(off, safe)
    tmp = path + ".incoming"
    written = 0
    try:
        with open(tmp, "wb") as f:
            for block in chunks:
                written += len(block)
                if written > max_bytes:
                    raise ValueError(
                        f"File too large (max. {max_bytes // 1024 // 1024} MB)."
                    )
                f.write(block)
    except BaseException:
        # Leave no half `.incoming` behind; the atomic swap below would be worthless otherwise.
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    os.replace(tmp, path)   # atomic: never expose half a file
    meta = read_metadata(path)
    return ForeignMod(
        filename=safe, enabled=False, size=written,
        mod_id=meta.get("mod_id", ""), name=meta.get("name", ""), version=meta.get("version", ""),
        loader=meta.get("loader", ""), game_versions=tuple(meta.get("game_versions", ())),
        problems=_problems(meta),
        dependencies=tuple(meta.get("dependencies", ())),
        unverifiable=str(meta.get("unverifiable", "")),
    )
