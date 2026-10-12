#!/bin/sh
# Nightly dump of the dashboard database.
#
# Runs as its own container in the stack (service `console-backup` in docker-compose.yml), so it
# needs neither a crontab entry on the host nor access to it. Schedule, retention, target
# directory and owner come from the .env (`DB_BACKUP_*`, see .env.example).
#
# Why it exists: the dashboard's weekly backup saves GAME WORLDS only. A volume failure would
# otherwise lose the audit log, player statistics and sessions, incidents, mod sets, schedules,
# users and roles — exactly what cannot be rebuilt from the game files.
#
# Three safeguards, each of them learned the hard way:
#
#   1. An aborted dump lies around looking like a complete file.
#      -> Write to `.part` first, rename after the checks.
#   2. A dump of a database without the application's tables runs without an error.
#      -> Check that the dump contains the schema (and a small sanity size).
#   3. The disk fills up and takes the next backup with it.
#      -> Remove old ones.
set -eu

DIR=${BACKUP_DIR:-/backups}
HOUR=${BACKUP_HOUR:-2}
MINUTE=${BACKUP_MINUTE:-30}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
# A table the application's schema always has. `pg_dump` of a database in which the backend
# has not created its tables succeeds and yields a few hundred bytes of valid gzip — a dump
# without this table is that case. The check is the content, not the size: a size floor that
# fits a database in use rejects every dump of a fresh install, whose first dumps are a few kB.
# Empty = skip the check.
REQUIRE_TABLE=${BACKUP_REQUIRE_TABLE-users}
# Sanity floor only, well below the dump of a fresh install.
MIN_BYTES=${BACKUP_MIN_BYTES:-1000}
# File name prefix. Clean-up only ever touches files with this prefix.
PREFIX=${BACKUP_PREFIX:-stacks-console}
# uid:gid the dumps are written as. Started as root, the script hands the target directory to
# this owner if root owns it (Docker creates a missing bind-mount source as root), then
# re-executes itself as the owner. Empty: stay as whoever started it.
OWNER=${BACKUP_OWNER:-}
# The dump at start is tried this often, this many seconds apart: on a first start this
# container is up before the backend has created its tables.
CATCHUP_TRIES=${BACKUP_CATCHUP_TRIES:-10}
CATCHUP_WAIT=${BACKUP_CATCHUP_WAIT:-30}

: "${PGHOST:=console-db}"
: "${PGUSER:=stacks}"
: "${PGDATABASE:=stacks}"
export PGHOST PGUSER PGDATABASE

say() { echo "[backup] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Busybox has no `date +%-H`, and "08" is not a valid number for the shell
# (octal). So strip leading zeros here.
num() { n=$(echo "$1" | sed 's/^0*//'); echo "${n:-0}"; }

dump_once() {
  ts=$(date +%Y%m%d-%H%M%S)
  target="$DIR/$PREFIX-$ts.sql.gz"
  part="$target.part"

  # The status of a pipeline is that of its LAST element — `gzip`. An aborted
  # `pg_dump` would pass as a success and leave a valid but incomplete file
  # behind. Busybox does not support `pipefail` reliably, so pg_dump records
  # its failure as a file.
  rm -f /tmp/pgdump.status
  { pg_dump --clean --if-exists 2>/tmp/pgdump.err || echo failed >/tmp/pgdump.status; } |
    gzip >"$part"
  if [ -f /tmp/pgdump.status ]; then
    say "ERROR pg_dump: $(tr '\n' ' ' </tmp/pgdump.err | tail -c 300)"
    rm -f "$part"
    return 1
  fi

  if ! gzip -t "$part" 2>/dev/null; then
    say "ERROR: the file is not valid gzip — discarded."
    rm -f "$part"
    return 1
  fi

  size=$(wc -c <"$part" | tr -d ' ')

  if [ -n "$REQUIRE_TABLE" ] &&
    ! gzip -dc "$part" | grep -Eq "^CREATE TABLE ([^ ]+\\.)?$REQUIRE_TABLE \\("; then
    say "ERROR: the dump has no table '$REQUIRE_TABLE' — the database holds no application schema (has the backend started yet?). Discarded."
    rm -f "$part"
    return 1
  fi

  if [ "$size" -lt "$MIN_BYTES" ]; then
    say "ERROR: only $size bytes (minimum $MIN_BYTES) — that is not a database dump. Discarded."
    rm -f "$part"
    return 1
  fi

  mv "$part" "$target"
  say "ok: $(basename "$target"), $size bytes"
}

clean_up() {
  # Leftovers of aborted runs: a dump that has been "running" for more than two
  # hours is not running any more.
  find "$DIR" -name '*.part' -mmin +120 -delete 2>/dev/null || true
  # **Only our own name pattern.** Dumps put next to them by hand are markers
  # taken before an intervention and are subject to no retention period.
  removed=$(find "$DIR" -name "$PREFIX-*.sql.gz" -mtime "+$KEEP_DAYS" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$removed" -gt 0 ]; then
    find "$DIR" -name "$PREFIX-*.sql.gz" -mtime "+$KEEP_DAYS" -delete 2>/dev/null || true
    say "removed $removed dump(s) older than $KEEP_DAYS days"
  fi
}

seconds_until_due() {
  h=$(num "$(date +%H)")
  m=$(num "$(date +%M)")
  s=$(num "$(date +%S)")
  now=$((h * 3600 + m * 60 + s))
  target=$((HOUR * 3600 + MINUTE * 60))
  diff=$((target - now))
  if [ "$diff" -le 0 ]; then diff=$((diff + 86400)); fi
  echo "$diff"
}

if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ -z "${BACKUP_AS_OWNER:-}" ]; then
  mkdir -p "$DIR"
  # Only a directory root owns: one the operator created for someone else is left alone.
  if [ "$(stat -c %u "$DIR")" = 0 ]; then
    chown "$OWNER" "$DIR"
  fi
  export BACKUP_AS_OWNER=1
  for tool in su-exec gosu; do
    if command -v "$tool" >/dev/null 2>&1; then
      exec "$tool" "$OWNER" /bin/sh "$0" "$@"
    fi
  done
  say "WARNING: neither su-exec nor gosu found — the dumps are written as root."
fi

mkdir -p "$DIR"
if [ ! -w "$DIR" ]; then
  say "ERROR: cannot write to $DIR as uid $(id -u) (the directory belongs to uid $(stat -c %u "$DIR")). Give it to this uid, for example: sudo chown $(id -u):$(id -g) <the host directory>"
  exit 1
fi
say "Start. Target $DIR, daily at $HOUR:$(printf '%02d' "$MINUTE") ($(date +%Z)), kept for $KEEP_DAYS days."

# Dump once at start if there is none from today yet. Otherwise a container
# that restarts every afternoon would never get a dump — and a stack that is
# deployed daily none at all.
today=$(date +%Y%m%d)
if [ -z "$(find "$DIR" -name "$PREFIX-$today-*.sql.gz" 2>/dev/null)" ]; then
  say "No dump from today yet — taking one right away."
  attempt=1
  until dump_once; do
    if [ "$attempt" -ge "$CATCHUP_TRIES" ]; then
      say "Catch-up dump failed, trying again at the scheduled time."
      break
    fi
    say "Catch-up dump failed (attempt $attempt of $CATCHUP_TRIES), trying again in $CATCHUP_WAIT s."
    attempt=$((attempt + 1))
    sleep "$CATCHUP_WAIT"
  done
  clean_up
fi

while :; do
  wait_s=$(seconds_until_due)
  say "next dump in $((wait_s / 3600)) h $(((wait_s % 3600) / 60)) min"
  sleep "$wait_s"
  dump_once || say "Dump failed — visible from the age of the newest file in $DIR."
  clean_up
done
