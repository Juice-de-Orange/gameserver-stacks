# Join packs for players

Put Modrinth modpacks (`.mrpack`) here; the join page offers them to players. No pack ships
with the repository.

**File name pattern: `stacks-<game version>.mrpack`.** `routers/join.py` looks the file up by
the version the Minecraft server is currently running. If none matches, the page does not offer
a download — it never invents a link that leads nowhere.

A pack contains only `modrinth.index.json`: loader, game version and, per mod, the file with
SHA1/SHA512, source URL and size. The Modrinth app downloads the mods themselves on import.

**The client and server builds of a mod must match** (for example Simple Voice Chat: the server
runs the Bukkit plugin, the client the Fabric mod, both with the same compatibility version).
When you update the server, add a new pack here — otherwise clients can no longer use it.

To build one: ask the Modrinth API for the matching build
(`api.modrinth.com/v2/project/<slug>/version`), write `modrinth.index.json` with file, hashes,
URL and size, then zip it and name it by the pattern above.
