#!/bin/sh
# Usage: steamcmd-download <app id> <workshop item id>
#
# Accepts exactly these two numbers and always logs in anonymously: the dashboard has no Steam
# account and must never be asked for one. steamcmd prints
#   Downloaded item <id> to "<path>"
# on success; the dashboard reads the path from that line.
set -eu

die() { echo "[steamcmd-download] FATAL: $*" >&2; exit 2; }

[ "$#" -eq 2 ] || die "expected <app id> <workshop item id>, got $# argument(s)"
case "$1$2" in
    *[!0-9]*|"") die "app id and workshop item id must be numbers" ;;
esac

home="${HOME:-/steam}"
[ -w "$home" ] || die "$home is not writable for uid $(id -u) — mount the cache there"

# First run: copy the bootstrap into the cache, where steamcmd may update itself.
if [ ! -x "$home/steamcmd/steamcmd.sh" ]; then
    mkdir -p "$home/steamcmd"
    cp -R /opt/steamcmd/. "$home/steamcmd/"
fi

exec "$home/steamcmd/steamcmd.sh" +login anonymous +workshop_download_item "$1" "$2" +quit
