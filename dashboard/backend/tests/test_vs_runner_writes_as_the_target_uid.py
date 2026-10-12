"""The Vintage Story runner writes nothing under the data directory as root.

Measured: the runner downloaded the game into `gamedata/server-bin` as root (10 661 root-owned
entries after the first start) and chowned only `VS_DATA_PATH`. "Remove and delete data" then
failed with HTTP 500, because the API (uid 1000) cannot delete root-owned files.

The test runs the real `entrypoint.sh` with stand-ins for the system tools on `PATH`: `id`,
`setpriv` and every tool that writes report the uid they ran as. No root and no network needed.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "images" / "vintagestory" / "entrypoint.sh"

# Each stand-in appends "<tool> uid=<the uid it runs as> <args>" to $CALLS. `setpriv` switches
# the pretended uid and execs the rest, like the real one.
_SHIMS = {
    "id": 'echo "${FAKE_UID:-0}"',
    "setpriv": (
        'uid=""; while [ "${1#--}" != "$1" ]; do case "$1" in --reuid=*) uid="${1#--reuid=}";; esac; shift; done\n'
        'echo "setpriv uid=${FAKE_UID:-0} -> $uid $*" >> "$CALLS"\n'
        'FAKE_UID="$uid" exec "$@"'
    ),
    "chown": 'echo "chown uid=${FAKE_UID:-0} $*" >> "$CALLS"',
    "curl": (
        'echo "curl uid=${FAKE_UID:-0} $*" >> "$CALLS"\n'
        'case "$*" in *.json*) echo \'{"9.9.9":{"linuxserver":{"urls":{"cdn":"https://cdn.example.com/s.tar.gz"},"md5":"abc"}}}\';;\n'
        '*) out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done; : > "$out";; esac'
    ),
    "md5sum": "exit 0",
    "tar": (
        'echo "tar uid=${FAKE_UID:-0} $*" >> "$CALLS"\n'
        'dest=""; while [ $# -gt 0 ]; do [ "$1" = "-C" ] && dest="$2"; shift; done; : > "$dest/VintagestoryServer.dll"'
    ),
    "dotnet": 'echo "dotnet uid=${FAKE_UID:-0} $*" >> "$CALLS"',
}


@pytest.fixture()
def run(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is needed to run the entrypoint")
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in _SHIMS.items():
        f = shims / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    # `jq` is real in the image; a stand-in that understands the two filters the script uses.
    jq = shims / "jq"
    jq.write_text(
        "#!/bin/sh\n"
        'in=$(cat)\n'
        'case "$*" in\n'
        '  *linuxserver*) echo \'{"urls":{"cdn":"https://cdn.example.com/s.tar.gz"},"md5":"abc"}\';;\n'
        '  *urls*) echo "https://cdn.example.com/s.tar.gz";;\n'
        '  *md5*) echo "abc";;\n'
        "esac\n"
    )
    jq.chmod(jq.stat().st_mode | stat.S_IEXEC)
    gamedata = tmp_path / "gamedata"
    calls = tmp_path / "calls.log"

    def _run(**env):
        full = {
            "PATH": f"{shims}:/usr/bin:/bin", "CALLS": str(calls), "VERSION": "9.9.9",
            "VS_DATA_PATH": str(gamedata / "vs"), "VS_BIN_CACHE": str(gamedata / "server-bin"),
            "PUID": "1234", "PGID": "1234", **env,
        }
        proc = subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return calls.read_text().splitlines()

    return _run


def test_root_only_prepares_then_everything_runs_as_the_target_uid(run):
    lines = run()
    tools = [line.split()[0] for line in lines]

    assert tools[0] == "chown", f"root must repair ownership first: {lines}"
    assert "1234:1234" in lines[0]
    assert tools[1] == "setpriv" and "-> 1234" in lines[1], f"then drop to PUID: {lines}"
    for line in lines[2:]:
        assert "uid=1234" in line, f"written as root after the drop: {line}"
    assert any(line.startswith("curl uid=1234") and "-o" in line for line in lines), "the download"
    assert any(line.startswith("tar uid=1234") for line in lines), "the unpacking"
    assert lines[-1].startswith("dotnet uid=1234"), f"the game: {lines}"


def test_a_container_that_already_runs_as_the_target_uid_is_left_alone(run):
    lines = run(FAKE_UID="1234")
    assert not any(line.startswith(("chown", "setpriv")) for line in lines), lines
    assert lines[-1].startswith("dotnet uid=1234")


def test_puid_zero_stays_root_without_a_loop(run):
    lines = run(PUID="0", PGID="0")
    assert not any(line.startswith("setpriv") for line in lines), lines
    assert lines[-1].startswith("dotnet uid=0")


def test_the_repair_is_limited_to_what_the_runner_writes(run, tmp_path):
    lines = run()
    chown = next(line for line in lines if line.startswith("chown"))
    # Only entries with another owner, only in the cache and the data path — `backups/` next to
    # them belongs to the API and is not the runner's business.
    assert os.path.join(str(tmp_path), "gamedata", "server-bin") in chown
    assert os.path.join(str(tmp_path), "gamedata", "vs") in chown
    assert "backups" not in chown
