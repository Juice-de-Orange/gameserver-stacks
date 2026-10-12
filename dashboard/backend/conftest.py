# Pins the rootdir so that `import app...` works from the backend/ directory.
#
# Also defines the command-line options for the `smoke` marker. They MUST live here, not in
# `tests/conftest.py`: pytest only collects `pytest_addoption` from the rootdir conftest.
import os

import pytest

# The settings default for JWT_SECRET is deliberately unusable (the API refuses to start with
# it, and PyJWT warns about its length). Tests sign with a compliant throw-away secret; this
# must be set before `app.config` is imported.
os.environ.setdefault("JWT_SECRET", "test-only-secret-" + "0" * 32)
# The same for the key of the stored game-server secrets (`config.secret_key_problem`).
os.environ.setdefault("GSS_SECRET_KEY", "test-only-key-" + "1" * 32)


def pytest_addoption(parser):
    """Options for `pytest -m smoke` — the assertions against a RUNNING system.

    `--admin-token` is optional. The three core assertions (health including the DB, a scoped
    route without a token = 401, the `/ws/logs` handshake without a token = 4401) deliberately need
    NO credentials, so they can be run unattended after every update.
    """
    g = parser.getgroup("smoke", "assertions against a running system")
    g.addoption(
        "--base-url", action="store", default=None,
        help="Base URL of the running dashboard, e.g. https://dashboard.example.com",
    )
    g.addoption(
        "--admin-token", action="store", default=None,
        help="Optional access token for the cases that need a signed-in session.",
    )


@pytest.fixture(scope="session")
def base_url(request) -> str:
    """Base URL without a trailing slash — or the test skips itself."""
    value = request.config.getoption("--base-url") or os.environ.get("GSS_SMOKE_BASE_URL")
    if not value:
        pytest.skip("--base-url / GSS_SMOKE_BASE_URL is missing (smoke tests only run against a running system).")
    return str(value).rstrip("/")


@pytest.fixture(scope="session")
def admin_token(request) -> str:
    """Access token, or empty. Tests that need one skip themselves."""
    return str(request.config.getoption("--admin-token") or os.environ.get("GSS_SMOKE_ADMIN_TOKEN") or "")
