import re

import pytest
from flask import session

import app as app_module
import security

VALID_ID = "b" * 22


class FakeSp:
    def artist(self, artist_id):
        return {"id": artist_id, "name": "Real Name"}


def test_token_is_encrypted_in_the_session():
    with app_module.app.test_request_context("/"):
        handler = app_module.token_cache()
        handler.save_token_to_cache({"access_token": "super-secret-token", "refresh_token": "r"})
        stored = session["token_info"]
        assert "super-secret-token" not in stored
        assert handler.get_cached_token()["access_token"] == "super-secret-token"


def test_tampered_or_foreign_token_counts_as_logged_out():
    with app_module.app.test_request_context("/"):
        session["token_info"] = "garbage"
        assert app_module.token_cache().get_cached_token() is None
        other = security.make_fernet("a-completely-different-secret-key-value")
        session["token_info"] = other.encrypt(b'{"access_token": "x"}').decode()
        assert app_module.token_cache().get_cached_token() is None


def test_security_headers_and_matching_csp_nonce(client):
    res = client.get("/")
    csp = res.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Cache-Control"] == "no-store"
    nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in res.get_data(as_text=True)


def test_nonce_changes_on_every_request(client):
    first = client.get("/").headers["Content-Security-Policy"]
    second = client.get("/").headers["Content-Security-Policy"]
    assert first != second


def test_cross_site_post_is_blocked(client):
    res = client.post("/api/playlist", json={}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and "blocked" in res.get_json()["error"]
    res = client.post("/api/playlist", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_same_site_post_is_allowed_through_the_origin_check(client):
    res = client.post("/api/playlist", json={}, headers={"Origin": "http://localhost"})
    assert res.status_code == 401  # reached the login check, so the origin check passed


def test_logout_is_post_only(client):
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 302


def test_artist_name_comes_from_spotify_not_the_browser(client, monkeypatch):
    seen = {}

    def fake_build(sp, artist, count, **kwargs):
        seen["artist"] = artist
        return ["u1"]

    monkeypatch.setattr(app_module, "get_client", lambda: FakeSp())
    monkeypatch.setattr(app_module.pl, "build_playlist", fake_build)
    monkeypatch.setattr(app_module.pl, "create_playlist", lambda *a, **k: "https://x")
    client.post("/api/playlist", json={"artist_id": VALID_ID, "artist_name": "Evil Name", "count": 1})
    assert seen["artist"] == {"id": VALID_ID, "name": "Real Name"}


def test_unexpected_errors_do_not_leak_details(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app_module, "get_client", lambda: FakeSp())
    monkeypatch.setattr(app_module.pl, "build_playlist", boom)
    res = client.post("/api/playlist", json={"artist_id": VALID_ID, "count": 3})
    assert res.status_code == 500
    assert "secret internal detail" not in res.get_data(as_text=True)
    assert res.is_json


def test_rate_limit_returns_json_429(client):
    app_module.limiter.enabled = True
    app_module.limiter.reset()
    codes = [client.get("/api/artists?q=abba").status_code for _ in range(100)]
    assert 429 in codes
    res = client.get("/api/artists?q=abba")
    assert res.is_json and "Too many" in res.get_json()["error"]


def test_health_endpoint(client):
    assert client.get("/health").data == b"ok"


GOOD_ENV = {
    "FLASK_SECRET_KEY": "x" * 40,
    "SPOTIPY_CLIENT_ID": "id",
    "SPOTIPY_CLIENT_SECRET": "secret",
    "SPOTIPY_REDIRECT_URI": "https://example.com/callback",
}


def test_production_config_accepts_good_settings():
    security.check_production_config(GOOD_ENV)


@pytest.mark.parametrize("change", [
    {"FLASK_SECRET_KEY": ""},
    {"FLASK_SECRET_KEY": "short"},
    {"SPOTIPY_CLIENT_ID": ""},
    {"SPOTIPY_REDIRECT_URI": "http://example.com/callback"},
])
def test_production_config_rejects_bad_settings(change):
    with pytest.raises(RuntimeError):
        security.check_production_config({**GOOD_ENV, **change})
