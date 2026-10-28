import os
import secrets

import requests
import spotipy
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session
from spotipy.cache_handler import FlaskSessionCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

import playlist as pl

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PRODUCTION") == "1",
)

SCOPE = "playlist-modify-public playlist-modify-private"
MAX_SONGS = 100
LASTFM_KEY = os.environ.get("LASTFM_API_KEY")
lastfm_session = requests.Session()


def make_oauth():
    return SpotifyOAuth(scope=SCOPE, cache_handler=FlaskSessionCacheHandler(session))


def get_client():
    """Spotify client for the logged-in user, or None. Tokens live in the user's own session."""
    if not FlaskSessionCacheHandler(session).get_cached_token():
        return None
    return spotipy.Spotify(auth_manager=make_oauth(), retries=5, status_retries=5, backoff_factor=0.5)


@app.get("/")
def index():
    sp, name = get_client(), None
    if sp:
        try:
            me = sp.current_user()
            name = me.get("display_name") or me["id"]
        except SpotifyException:
            session.clear()
            sp = None
    return render_template("index.html", logged_in=bool(sp), user_name=name)


@app.get("/login")
def login():
    session["oauth_state"] = secrets.token_urlsafe(16)  # CSRF protection
    return redirect(make_oauth().get_authorize_url(state=session["oauth_state"]))


@app.get("/callback")
def callback():
    if request.args.get("error") or request.args.get("state") != session.pop("oauth_state", None):
        return redirect("/")
    make_oauth().get_access_token(request.args.get("code"), as_dict=False)
    return redirect("/")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/artists")
def api_artists():
    sp = get_client()
    if not sp:
        return jsonify(error="Log in with Spotify first."), 401
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(artists=[])
    try:
        items = sp.search(q=q, type="artist", limit=8)["artists"]["items"]
    except SpotifyException as e:
        return jsonify(error=f"Spotify error: {e.msg}"), 502
    artists = [
        {"id": a["id"], "name": a["name"], "image": a["images"][-1]["url"] if a.get("images") else None}
        for a in items
    ]
    return jsonify(artists=artists)


@app.post("/api/playlist")
def api_playlist():
    sp = get_client()
    if not sp:
        return jsonify(error="Log in with Spotify first."), 401
    data = request.get_json(silent=True) or {}
    artist = {"id": data.get("artist_id"), "name": data.get("artist_name")}
    if not artist["id"] or not artist["name"]:
        return jsonify(error="Pick an artist first."), 400
    try:
        count = int(data.get("count", 20))
    except (TypeError, ValueError):
        return jsonify(error="Song count must be a number."), 400
    if not 1 <= count <= MAX_SONGS:
        return jsonify(error=f"Choose between 1 and {MAX_SONGS} songs."), 400

    try:
        uris = pl.build_playlist(
            sp, artist, count,
            lastfm_key=LASTFM_KEY,
            exclude_variants=bool(data.get("exclude_variants", True)),
            session=lastfm_session,
        )
        if not uris:
            return jsonify(error="No tracks found for that artist."), 404
        url = pl.create_playlist(sp, artist["name"], uris, public=bool(data.get("public", False)))
    except SpotifyException as e:
        return jsonify(error=f"Spotify error: {e.msg}"), 401 if e.http_status == 401 else 502
    return jsonify(url=url, added=len(uris), requested=count)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
