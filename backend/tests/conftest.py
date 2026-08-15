from datetime import datetime, timezone
from pathlib import Path

import pytest

from pensieve.config import Settings
from pensieve.db import connect, init_db
from pensieve.ratelimit import auth_limiter, request_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Give every test a fresh rate-limit budget.

    The limiters are module-level singletons (one process, one container --
    see ``pensieve/ratelimit.py``), so without this they carry hits across
    tests. Every unauthenticated test hits the same "testclient" key, which
    made assertions like "a tampered cookie is a 401" fail as a 429 depending
    on collection order -- a real, reproducible flake under pytest-randomly.
    """
    auth_limiter._hits.clear()
    request_limiter._hits.clear()
    yield
    auth_limiter._hits.clear()
    request_limiter._hits.clear()

_UPSERT_USER = """
INSERT INTO users (plex_account_id, name, role, last_seen, revoked)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(plex_account_id) DO UPDATE SET
    name = excluded.name, role = excluded.role, revoked = excluded.revoked
"""


def seed_user(
    settings: Settings, *, user_id: int, name: str = "Sam",
    role: str = "member", revoked: int = 0,
) -> None:
    """Insert (or refresh) a ``users`` row for a test session.

    ``current_user`` is DB-authoritative since the session-revocation fix: a
    signed cookie with no matching row is a 401, so every test that fakes a
    login has to seed the row too. Calls ``init_db`` first so this works
    whether or not the app's lifespan has run yet.
    """
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        conn.execute(
            _UPSERT_USER,
            (user_id, name, role, datetime.now(timezone.utc).isoformat(), revoked),
        )
    finally:
        conn.close()


#: A throwaway VAPID keypair, generated for the test suite only and never
#: used by any deployment. It has to be a *valid* P-256 key rather than a
#: placeholder string: tests that let ``push.send_to_user`` reach
#: ``pywebpush`` sign a real VAPID header before the mocked transport ever
#: sees the request. That validity is also why a secret scanner flags it,
#: hence the allow markers -- generate your own with
#: ``vapid --gen`` (py-vapid) rather than reusing these.
_TEST_VAPID_PRIVATE_KEY = "W6ZEReQJWKq4uypp43X6OV6tOMIiLoGeiCYpHI0Sd94"  # gitleaks:allow
_TEST_VAPID_PUBLIC_KEY = "BCIlbEhlcRv6J3z4oq9ZpjK0pS2o7ts1_TBttZFVtkdS1_quoZ1J9s-fOS7JJfPWxEwiNcsNY1jQKN0OuiwLuio"  # gitleaks:allow


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings with safe test defaults; DB in tmp_path."""
    defaults = dict(
        # >= 32 chars: Settings enforces a floor on the cookie-signing key.
        session_secret="test-secret-test-secret-test-secret",
        base_url="https://example.test",
        plex_client_id="pensieve-test",
        plex_server_machine_id="machine-123",
        plex_owner_account_id=1,
        plex_owner_token="ot",
        radarr_api_key="rk", sonarr_api_key="sk", jellyseerr_api_key="jk",
        radarr_profile_hd_id=6, radarr_profile_4k_id=7,
        sonarr_profile_hd_id=4, sonarr_profile_4k_id=5,
        sonarr_profile_720_id=3,
        db_path=str(tmp_path / "test.db"),
        vapid_private_key=_TEST_VAPID_PRIVATE_KEY,
        vapid_public_key=_TEST_VAPID_PUBLIC_KEY,
        vapid_subject="mailto:test@example.com",
    )
    defaults.update(overrides)
    return Settings(**defaults)
