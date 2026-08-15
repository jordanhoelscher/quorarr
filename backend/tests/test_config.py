"""Boot-time configuration guards.

Everything here is about a *deployment* being wrong, not a request being
wrong. The bias is to fail at startup: a misconfigured instance that boots is
one whose first symptom lands on a user (a forgeable cookie, a push that
never arrives, an auth token forwarded to someone else's host).
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pensieve.config import Settings
from pensieve.main import create_app
from tests.conftest import make_settings


def _kwargs(tmp_path, **overrides) -> dict:
    """The settings kwargs ``make_settings`` uses, as a plain dict."""
    base = make_settings(tmp_path).model_dump()
    base.update(overrides)
    return base


# --- session secret ----------------------------------------------------------


@pytest.mark.parametrize("secret", ["", "x", "short-secret"])
def test_short_session_secret_refuses_to_boot(tmp_path, secret):
    """A guessable signing key is a forgeable owner session."""
    with pytest.raises(ValidationError):
        Settings(**_kwargs(tmp_path, session_secret=secret))


def test_thirty_two_char_session_secret_is_accepted(tmp_path):
    Settings(**_kwargs(tmp_path, session_secret="a" * 32))


# --- base url ----------------------------------------------------------------


def test_base_url_is_required(tmp_path):
    """No default: the wrong one forwards a friend's auth token elsewhere."""
    kwargs = _kwargs(tmp_path)
    del kwargs["base_url"]
    with pytest.raises(ValidationError):
        Settings(**kwargs)


# --- push --------------------------------------------------------------------


def test_vapid_keys_without_a_subject_refuse_to_boot(tmp_path):
    with pytest.raises(ValidationError, match="VAPID_SUBJECT"):
        Settings(**_kwargs(tmp_path, vapid_subject=""))


def test_push_disabled_entirely_is_a_supported_deployment(tmp_path):
    """No keys, no subject: push degrades to Discord rather than erroring."""
    settings = Settings(
        **_kwargs(tmp_path, vapid_private_key="", vapid_public_key="", vapid_subject="")
    )
    assert settings.vapid_private_key == ""


def test_vapid_subject_has_no_personal_default():
    """A default here is mailed to Apple/Google/Mozilla by every deployment."""
    assert Settings.model_fields["vapid_subject"].default == ""


# --- quality lanes -----------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "radarr_profile_hd_id",
        "radarr_profile_4k_id",
        "sonarr_profile_hd_id",
        "sonarr_profile_4k_id",
    ],
)
def test_a_required_profile_id_of_zero_refuses_to_boot(tmp_path, field):
    """0 is not a profile id, it is an unset environment variable.

    Letting it through files every request against whatever the arr made of
    ``profileId: 0`` -- a silently wrong quality lane rather than an error.
    """
    with pytest.raises(ValidationError):
        Settings(**_kwargs(tmp_path, **{field: 0}))


def test_the_optional_720_lane_may_be_unset(tmp_path):
    """Not every deploy has a space-saver profile; that lane just closes.

    The request route then answers 502 "720p lane not configured" rather
    than quietly filing at 1080p (see test_discover_routes).
    """
    assert Settings(**_kwargs(tmp_path, sonarr_profile_720_id=0)).sonarr_profile_720_id == 0


# --- branding ----------------------------------------------------------------


def test_branding_defaults_are_neutral():
    assert Settings.model_fields["app_name"].default == "Quorarr"
    assert Settings.model_fields["owner_name"].default == "the owner"
    assert Settings.model_fields["server_name"].default == ""


def test_config_endpoint_serves_branding_and_client_id(tmp_path):
    """One public payload the SPA boots from: name, owner, Plex client id."""
    settings = make_settings(tmp_path, app_name="the app", owner_name="Ada")
    client = TestClient(create_app(settings))
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app_name"] == "the app"
    assert body["owner_name"] == "Ada"
    assert body["client_id"] == settings.plex_client_id


def test_config_endpoint_leaks_no_secrets(tmp_path):
    """It is unauthenticated: the payload is a fixed allowlist, not a dump."""
    client = TestClient(create_app(make_settings(tmp_path)))
    assert set(client.get("/api/config").json()) == {
        "app_name", "owner_name", "server_name", "client_id", "version",
    }


def test_client_id_endpoint_still_answers(tmp_path):
    """Kept for a service-worker-cached bundle from before /api/config."""
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings))
    assert client.get("/api/auth/client-id").json() == {
        "client_id": settings.plex_client_id
    }


# --- PWA manifest ------------------------------------------------------------


def test_manifest_takes_its_name_from_app_name(tmp_path):
    """The home-screen name has to follow config, and it is a static file.

    Served from a route rather than ``frontend/public`` so that rebranding an
    instance does not mean rebuilding the image.
    """
    client = TestClient(create_app(make_settings(tmp_path, app_name="the app")))
    resp = client.get("/manifest.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "the app"
    assert body["short_name"] == "the app"
    assert [icon["src"] for icon in body["icons"]] == [
        "/icons/icon-192.png",
        "/icons/icon-512.png",
    ]


def test_generated_manifest_wins_over_the_bundled_static_one(tmp_path):
    """The route has to beat the StaticFiles mount, or config never reaches it.

    The built frontend ships its own ``manifest.json`` in ``dist/``. The mount
    is at "/" and is added last, so the explicit route matches first -- this
    pins that ordering, which is otherwise invisible and easy to undo.
    """
    static = tmp_path / "static"
    static.mkdir()
    (static / "manifest.json").write_text('{"name": "from the bundle"}')
    (static / "index.html").write_text("<!doctype html>")
    client = TestClient(
        create_app(make_settings(tmp_path, app_name="Pensieve", static_dir=str(static)))
    )
    assert client.get("/manifest.json").json()["name"] == "Pensieve"


# --- packaging ---------------------------------------------------------------


def test_pyproject_does_not_hardcode_a_version():
    """One canonical version: ``pensieve.__version__``.

    The packaging metadata sat at 0.1.0 through nine releases while /health
    and the UI footer reported the truth. Reading it dynamically is what
    stops a published image tag from meaning nothing.
    """
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "pensieve.__version__"
    }
