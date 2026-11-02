from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def _client(tmp_path, services, with_dist: bool):
    dist = tmp_path / "dist"
    if with_dist:
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>SPA-SHELL</html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    settings = make_settings(tmp_path / "data", frontend_dist=dist)
    return TestClient(create_app(settings, services))


def test_built_ui_is_served_with_spa_fallback(tmp_path, services):
    with _client(tmp_path, services, with_dist=True) as c:
        assert "SPA-SHELL" in c.get("/").text
        assert "SPA-SHELL" in c.get("/w/ws_123/summary").text  # client-side route -> index.html
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/health").json()["status"] == "ok"  # API still wins over the catch-all


def test_unknown_api_routes_stay_json_404_not_html(tmp_path, services):
    with _client(tmp_path, services, with_dist=True) as c:
        r = c.get("/api/does-not-exist")
        assert r.status_code == 404 and "SPA-SHELL" not in r.text


def test_static_serving_cannot_escape_dist(tmp_path, services):
    with _client(tmp_path, services, with_dist=True) as c:
        for p in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
            assert "SECRET" not in c.get(p).text


def test_no_dist_means_api_only(tmp_path, services):
    with _client(tmp_path, services, with_dist=False) as c:
        assert c.get("/").status_code == 404
        assert c.get("/api/health").status_code == 200
