from app.auth import perm
import pytest
from fastapi import HTTPException

from app.models import PERMISSION_FLAGS, Role, User


def _user(role: Role, **flags) -> User:
    u = User(display_name="x", username="x", pw_hash="x", role=role)
    for f in PERMISSION_FLAGS:
        setattr(u, f, flags.get(f, False))
    return u


def _empty_matrix():
    perm._MATRIX = {r: {f: False for f in PERMISSION_FLAGS} for r in Role}


def test_admin_bypasses_all_flags():
    _empty_matrix()
    assert perm.has_permission(_user(Role.admin), "can_manage_users")


def test_role_default_and_user_override():
    _empty_matrix()
    perm._MATRIX[Role.trusted]["can_view_logs"] = True
    assert perm.has_permission(_user(Role.trusted), "can_view_logs")
    assert not perm.has_permission(_user(Role.member), "can_view_logs")
    # A per-user grant is additive:
    assert perm.has_permission(_user(Role.member, can_view_logs=True), "can_view_logs")


def test_a_temp_password_only_reaches_change_password_at_the_api():
    """`must_change_pw` must hold at the API, not only in the browser (`App.tsx` redirects).

    Anyone holding a temp password (everyone with `can_manage_users` can issue one) could
    otherwise sign in via `POST /api/auth/login` and use the access token directly against the
    API: every route the account has rights for answers normally, the temp password is never
    consumed, never expires, and a `refresh` extends the access indefinitely.

    The allow list is the delicate part: too narrow, and the account cannot change its password
    and IS locked out. That is why it is checked FIRST here.
    """
    from types import SimpleNamespace

    from app.auth import deps

    def _req(path):
        return SimpleNamespace(url=SimpleNamespace(path=path))

    fresh = _user(Role.member)
    fresh.must_change_pw = True

    # The allow list: without these two the account is locked out.
    for allowed in ("/api/auth/me", "/api/auth/change-password"):
        deps._enforce_pw_change(fresh, _req(allowed))   # must NOT raise

    # Everything else is closed.
    for locked in ("/api/servers", "/api/servers/pz-1/logs", "/api/users",
                     "/api/servers/pz-1/backups"):
        with pytest.raises(HTTPException) as ei:
            deps._enforce_pw_change(fresh, _req(locked))
        assert ei.value.status_code == 403
        assert ei.value.detail["code"] == "auth.password_change_required"

    # Without the flag nothing changes.
    normal = _user(Role.member)
    normal.must_change_pw = False
    deps._enforce_pw_change(normal, _req("/api/servers"))


def test_a_temp_password_opens_no_websocket():
    """Without this check /ws/console, /ws/logs and /ws/cross-chat would stay open, and with
    them the RCON console. There is no allow list there because there is nothing to allow."""
    import inspect

    from app.auth import deps

    source = inspect.getsource(deps._ws_user)
    assert "must_change_pw" in source, (
        "_ws_user does not check the flag; the WebSockets stay open for an account with a "
        "temp password."
    )


def test_backup_download_requires_admin_like_the_restore():
    """The archive contains the passwords in plain text, so the download is a privilege path.

    A PZ backup contains `Server/<name>.ini` with `Password` AND `RCONPassword` set: the image
    writes both from the container env into the file on every boot, so they land in every
    backup.

    `restore_backup` requires `require_admin` (destructive); `download_backup` must too. Whoever
    may download the archive reads the RCON password from it, and **RCON is the full server
    console**: `can_console_full` is admin-only, and via the archive the flag would not be
    needed. On top comes the server's join password.

    Checked on the real dependency tree, not on the text, just like `test_auth_order.py`.
    """
    from fastapi.dependencies.utils import get_dependant

    from app.auth.deps import require_admin
    from app.main import app
    from tests.route_walk import flat_routes

    def _flatten(dep):
        out = [dep.call]
        for sub in dep.dependencies:
            out.extend(_flatten(sub))
        return out

    found = {}
    for r in flat_routes(app):
        name = getattr(r, "name", "")
        if name in ("download_backup", "restore_backup"):
            found[name] = _flatten(r.dependant)

    assert "download_backup" in found, "route not found; does the collection still work?"
    assert require_admin in found["restore_backup"], "precondition: restore is admin-only"
    assert require_admin in found["download_backup"], (
        "The backup download does not depend on require_admin. The archive contains Password "
        "and RCONPassword in plain text; whoever downloads it has the full server console "
        "without holding can_console_full."
    )


def test_the_ui_knows_every_permission_flag():
    """`Users.tsx` renders the permission chips from its OWN table, so it must be complete.

    A flag missing from `FLAG_LABEL` in `pages/Users.tsx` can be neither seen nor granted per
    user, only via the role matrix or curl: an admin who wants to give one person a specific
    right has no way to it in the UI and no hint that the right exists at all.

    The guard sits in the BACKEND because only here both sides are readable: the frontend
    cannot import `PERMISSION_FLAGS`, and a hand-maintained copy is exactly the problem.
    """
    import re
    from pathlib import Path

    users_tsx = Path(__file__).parent.parent.parent / "frontend" / "src" / "pages" / "Users.tsx"
    assert users_tsx.exists(), f"{users_tsx} no longer exists; update the path in this guard"
    source = users_tsx.read_text(encoding="utf-8")

    block = re.search(r"const FLAG_LABEL[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL)
    assert block, "FLAG_LABEL not found; is the table still called that?"
    # Only real entries, no comment lines.
    in_frontend = {
        m.group(1)
        for line in block.group(1).splitlines()
        if not line.lstrip().startswith("//")
        for m in [re.match(r"\s*(can_[a-z_]+)\s*:", line)]
        if m
    }

    missing = sorted(set(PERMISSION_FLAGS) - in_frontend)
    extra = sorted(in_frontend - set(PERMISSION_FLAGS))
    assert not missing, (
        "The backend knows these permissions but the user management does not; they can be "
        f"neither seen nor granted per user: {missing}"
    )
    assert not extra, (
        "The UI offers these permissions but the backend does not know them (typo or "
        f"leftover): {extra}"
    )
