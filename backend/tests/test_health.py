from fastapi.testclient import TestClient

from pensieve import __version__
from pensieve.main import create_app
from tests.conftest import make_settings


def test_health_reports_version(tmp_path):
    app = create_app(make_settings(tmp_path))
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_api_docs_routes_are_disabled(tmp_path):
    """No public schema map for a delete-files-from-disk API.

    ``/openapi.json`` enumerates every admin route and request body; on a
    public vhost that is a free blueprint, and nothing gates it (the session
    dependency is per-router, these are framework routes).
    """
    app = create_app(make_settings(tmp_path))
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
