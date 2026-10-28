import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


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
    monkeypatch.setattr(app_module, "get_client", lambda: object())
    assert client.post("/api/playlist", json={"count": 5}).status_code == 400
    body = {"artist_id": "a", "artist_name": "A"}
    assert client.post("/api/playlist", json={**body, "count": "abc"}).status_code == 400
    assert client.post("/api/playlist", json={**body, "count": 0}).status_code == 400
    assert client.post("/api/playlist", json={**body, "count": 101}).status_code == 400


def test_playlist_happy_path(client, monkeypatch):
    monkeypatch.setattr(app_module, "get_client", lambda: object())
    monkeypatch.setattr(app_module.pl, "build_playlist", lambda *a, **k: ["u1", "u2"])
    monkeypatch.setattr(app_module.pl, "create_playlist", lambda *a, **k: "https://open.spotify.com/playlist/x")
    res = client.post("/api/playlist", json={"artist_id": "a", "artist_name": "A", "count": 5})
    assert res.get_json() == {"url": "https://open.spotify.com/playlist/x", "added": 2, "requested": 5}
