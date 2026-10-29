import time

import app as app_module

VALID_ID = "a" * 22


class FakeSp:
    def artist(self, artist_id):
        return {"id": artist_id, "name": "Real Name"}


def test_logged_out_home_shows_connect_button(client):
    res = client.get("/")
    assert res.status_code == 200 and b"Connect Spotify" in res.data


def test_api_requires_login(client):
    assert client.get("/api/artists?q=abba").status_code == 401
    assert client.post("/api/playlist", json={}).status_code == 401


def test_callback_rejects_bad_state(client):
    res = client.get("/callback?code=x&state=wrong")
    assert res.status_code == 302 and res.headers["Location"] == "/"


def test_playlist_validates_input(client, monkeypatch):
    monkeypatch.setattr(app_module, "get_client", lambda: FakeSp())
    post = lambda body: client.post("/api/playlist", json=body)
    assert post({"count": 5}).status_code == 400
    assert post({"artist_id": "not-valid!", "count": 5}).status_code == 400
    assert post({"artist_id": VALID_ID, "count": "abc"}).status_code == 400
    assert post({"artist_id": VALID_ID, "count": 0}).status_code == 400
    assert post({"artist_id": VALID_ID, "count": 101}).status_code == 400
    assert client.post("/api/playlist", data="not json", content_type="text/plain").status_code == 400


def test_playlist_happy_path(client, monkeypatch):
    monkeypatch.setattr(app_module, "get_client", lambda: FakeSp())
    monkeypatch.setattr(app_module.pl, "build_playlist", lambda *a, **k: ["u1", "u2"])
    monkeypatch.setattr(app_module.pl, "create_playlist", lambda *a, **k: "https://open.spotify.com/playlist/x")
    res = client.post("/api/playlist", json={"artist_id": VALID_ID, "count": 5})
    assert res.get_json() == {"url": "https://open.spotify.com/playlist/x", "added": 2, "requested": 5}


def test_client_works_outside_request_context(monkeypatch):
    """Regression: playlist lookups run in worker threads, which have no Flask session."""
    from flask import session

    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:5000/callback")
    with app_module.app.test_request_context("/"):
        app_module.token_cache().save_token_to_cache({
            "access_token": "abc", "refresh_token": "r", "token_type": "Bearer",
            "expires_in": 3600, "expires_at": int(time.time()) + 3600, "scope": app_module.SCOPE,
        })
        sp = app_module.get_client()
    assert sp is not None
    assert sp._auth_headers() == {"Authorization": "Bearer abc"}  # no request context here


def test_playlist_name_is_passed_to_creation(client, monkeypatch):
    seen = {}

    def fake_create(sp, artist_name, uris, public=False, name=None):
        seen["name"] = name
        return "https://x"

    monkeypatch.setattr(app_module, "get_client", lambda: FakeSp())
    monkeypatch.setattr(app_module.pl, "build_playlist", lambda *a, **k: ["u1"])
    monkeypatch.setattr(app_module.pl, "create_playlist", fake_create)
    client.post("/api/playlist", json={"artist_id": VALID_ID, "count": 1, "name": "Road trip"})
    assert seen["name"] == "Road trip"
    client.post("/api/playlist", json={"artist_id": VALID_ID, "count": 1})
    assert seen["name"] is None
