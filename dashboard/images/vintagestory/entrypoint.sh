#!/bin/bash
# Fetch the Vintage Story server named in $VERSION (once, md5-checked, cached), then run it.
#
# Any failure exits non-zero BEFORE the game starts. That is deliberate: the dashboard's
# version change only counts a boot as good once the game answers or logs its ready line, so a
# failed fetch ends in its regular rollback instead of a server quietly running some other version.
set -euo pipefail

INDEX_URLS=(https://api.vintagestory.at/stable.json https://api.vintagestory.at/unstable.json)
CACHE="${VS_BIN_CACHE:-/gamedata/server-bin}"
KEEP="${VS_KEEP_VERSIONS:-3}"
MARKER=".vs-complete"

say() { echo "[vs-runner] $*"; }
die() { echo "[vs-runner] FATAL: $*" >&2; exit 1; }

# Everything this script and the game write under /gamedata belongs to PUID:PGID, the uid of the
# dashboard API, which edits serverconfig.json, installs mods and deletes the data directory when
# a server is removed. Root does only this preparation, then the script runs again as that uid:
# the download, the version cache and the game itself never create a root-owned file.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
if [ "$(id -u)" = 0 ] && [ "$PUID" != 0 ]; then
    mkdir -p "$CACHE" "$VS_DATA_PATH"
    # Repairs what older images or a root-run server left behind (a root-owned server-bin made
    # "Remove and delete data" fail). Only entries with another owner are touched, so a normal
    # boot changes nothing.
    find "$CACHE" "$VS_DATA_PATH" \( ! -user "$PUID" -o ! -group "$PGID" \) \
        -exec chown -h "$PUID:$PGID" {} +
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$BASH" "$0" "$@"
fi

VERSION="${VERSION:-}"
[ -n "$VERSION" ] || die "VERSION is not set — this image has no default game version."
# The version becomes a path component below; refuse anything that is not a plain version.
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] \
    || die "VERSION '$VERSION' does not look like a Vintage Story version."

dir="$CACHE/$VERSION"

fetch() {
    local url="" md5="" idx
    for idx in "${INDEX_URLS[@]}"; do
        local entry
        entry=$(curl -fsSL --retry 3 --max-time 30 "$idx" \
                | jq -c --arg v "$VERSION" '.[$v].linuxserver // empty') \
            || { say "index $idx not reachable"; continue; }
        if [ -n "$entry" ]; then
            url=$(jq -r '.urls.cdn // .urls.local // empty' <<<"$entry")
            md5=$(jq -r '.md5 // empty' <<<"$entry")
            break
        fi
    done
    [ -n "$url" ] && [ -n "$md5" ] \
        || die "version $VERSION has no linux server build in the official index."

    local tmp
    tmp=$(mktemp -d "$CACHE/.fetch-$VERSION.XXXXXX")
    # Clean up a half-finished fetch on any exit from this function's failure paths.
    trap 'rm -rf "$tmp"' EXIT
    say "downloading $url"
    curl -fSL --retry 3 --max-time 900 -o "$tmp/server.tar.gz" "$url" \
        || die "download of $VERSION failed."
    echo "$md5  $tmp/server.tar.gz" | md5sum -c --status \
        || die "md5 mismatch for $VERSION — refusing to run an unverified server."
    mkdir "$tmp/game"
    tar -xzf "$tmp/server.tar.gz" -C "$tmp/game" || die "extracting $VERSION failed."
    [ -f "$tmp/game/VintagestoryServer.dll" ] || die "archive for $VERSION has no VintagestoryServer.dll."
    touch "$tmp/game/$MARKER"
    rm -rf "$dir"
    mv "$tmp/game" "$dir"
    rm -rf "$tmp"
    trap - EXIT
    say "installed $VERSION into $dir"
}

prune() {
    # Keep the $KEEP most recently used versions (the current one is always among them), so a
    # rollback still works if the download server is unreachable.
    local old
    while IFS= read -r old; do
        [ "$old" = "$dir" ] && continue
        say "removing cached version $(basename "$old")"
        rm -rf "$old"
    done < <(find "$CACHE" -mindepth 1 -maxdepth 1 -type d ! -name '.fetch-*' -printf '%T@ %p\n' \
             | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2-)
    # Leftovers of interrupted fetches.
    find "$CACHE" -mindepth 1 -maxdepth 1 -type d -name '.fetch-*' -exec rm -rf {} + || true
}

mkdir -p "$CACHE"
if [ -f "$dir/$MARKER" ]; then
    say "using cached $VERSION"
else
    fetch
fi
touch "$dir"   # marks it as recently used for prune()
prune

mkdir -p "$VS_DATA_PATH"
cd "$dir"
export HOME="$VS_DATA_PATH"
exec dotnet VintagestoryServer.dll --dataPath "$VS_DATA_PATH"
