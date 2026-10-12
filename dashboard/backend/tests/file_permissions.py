"""Whether POSIX file permissions have any effect on THIS system — measured, not guessed.

**Why this file exists.** Several guards make something unreadable with `os.chmod(path, 0o000)`
and check that the code does not pass the read error off as "empty". On Windows `chmod` does not
revoke read permission — it only flips the read-only bit — so those tests could **never** turn
green there. A gate that is chronically red looks like no gate at all.

**The check asks for the capability, not for the operating system.** `os.name == "nt"` would be
the convenient shortcut and the wrong statement: as `root` a file with `0o000` is readable too,
and a Windows that enforces POSIX permissions one day would wrongly keep being skipped. So what
the system really does is tried once at collection time.

**This is a skip, not an acquittal** — and it is named so that it does not pass as "green". On
Linux the guards run unchanged.
"""
from __future__ import annotations

import os
import pathlib
import stat
import tempfile

import pytest


def _measure() -> tuple[bool, bool]:
    """(does chmod revoke read permission?, is the mode that was set reported back?)"""
    revokes = False
    mode = False
    try:
        with tempfile.TemporaryDirectory() as d:
            probe = pathlib.Path(d) / "probe"
            probe.write_text("x", encoding="utf-8")
            try:
                os.chmod(probe, 0o600)
                mode = stat.S_IMODE(probe.stat().st_mode) == 0o600
                os.chmod(probe, 0o000)
                try:
                    probe.read_text(encoding="utf-8")
                except OSError:
                    revokes = True
            finally:
                # otherwise cleaning up the temp directory fails
                try:
                    os.chmod(probe, 0o600)
                except OSError:
                    pass
    except OSError:
        pass
    return revokes, mode


REVOKING_WORKS, FILE_MODE_WORKS = _measure()

#: For guards that make a file/folder UNREADABLE and expect the read error.
ONLY_WITH_PERMISSION_REVOCATION = pytest.mark.skipif(
    not REVOKING_WORKS,
    reason="`os.chmod(…, 0o000)` does not revoke read permission on this system "
           "(Windows, or running as root) — the guard could never turn green here. "
           "See tests/file_permissions.py.",
)

#: For guards that check that the code sets a file to `0o600`.
ONLY_WITH_FILE_MODE = pytest.mark.skipif(
    not FILE_MODE_WORKS,
    reason="this system does not report back the mode set with `os.chmod` "
           "(Windows) — the guard could never turn green here. "
           "See tests/file_permissions.py.",
)
