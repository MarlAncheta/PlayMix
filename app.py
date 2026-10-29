import logging
import os
import re
import secrets

import requests
import spotipy
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import playlist as pl
import security

load_dotenv()
logging.basicConfig(level=logging.INFO)

PRODUCTION = os.environ.get("PRODUCTION") == "1"
if PRODUCTION:
    security.check_production_config(os.environ)  # fail at startup, not on the first login

SCOPE = "playlist-modify-public playlist-modify-private"
MAX_SONGS = 100
SEARCH_LIMIT = "90 per minute"
PLAYLIST_LIMIT = "15 per hour"
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
LASTFM_KEY = os.environ.get("LASTFM_API_KEY")
lastfm_session = requests.Session()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=PRODUCTION,
    SESSION_COOKIE_NAME="__Host-session" if PRODUCTION else "session",
    MAX_CONTENT_LENGTH=16 * 1024,  # requests here are tiny; reject anything large
)
if PRODUCTION:
    # Render sits behind a proxy; trust one hop so client IPs and https are detected correctly
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

security.init_security(app, PRODUCTION)
limiter = Limiter(key_func=get_remote_address, app=app, storage_uri="memory://")
fernet = security.make_fernet(app.secret_key)


def token_cache():
    return security.EncryptedSessionCacheHandler(session, fernet)


def make_oauth():
    return SpotifyOAuth(scope=SCOPE, cache_handler=token_cache())


def get_client():
    """Spotify client for the logged-in user, or None.

    The token is read (and refreshed if needed) here, in the request thread, then handed to
    the client as a plain string so worker threads never need Flask's session.
    """
    token_info = token_cache().get_cached_token()
    if not token_info:
        return None
    token_info = make_oauth().validate_token(token_info)  # refreshes and re-saves if expired
    if not token_info:
        return None
    return spotipy.Spotify(auth=token_info["access_token"], retries=5, status_retries=5, backoff_factor=0.5)


@app.errorhandler(HTTPException)
def http_error(e):
    if request.path.startswith("/api/"):
        if e.code == 429:
            return jsonify(error="Too many requests. Please wait a bit and try again."), 429
        return jsonify(error=e.description), e.code
    return e


@app.errorhandler(Exception)
def unexpected_error(e):
    app.logger.exception("Unhandled error")  # details stay in the server logs only
    if request.path.startswith("/api/"):
        return jsonify(error="Something went wrong on our side. Please try again."), 500
    return "Something went wrong. Please try again.", 500


@app.get("/health")
@limiter.exempt
def health():
    return "ok"


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
    code = request.args.get("code")
    if request.args.get("error") or not code or request.args.get("state") != session.pop("oauth_state", None):
        return redirect("/")
    try:
        make_oauth().get_access_token(code, as_dict=False)
    except (SpotifyOauthError, SpotifyException):
        app.logger.warning("Spotify login failed")
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/artists")
@limiter.limit(SEARCH_LIMIT)
def api_artists():
    sp = get_client()
    if not sp:
        return jsonify(error="Log in with Spotify first."), 401
    q = request.args.get("q", "").strip()[:100]
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
@limiter.limit(PLAYLIST_LIMIT)
def api_playlist():
    sp = get_client()
    if not sp:
        return jsonify(error="Log in with Spotify first."), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid request."), 400

    artist_id = data.get("artist_id")
    if not isinstance(artist_id, str) or not SPOTIFY_ID.fullmatch(artist_id):
        return jsonify(error="Pick an artist first."), 400
    try:
        count = int(data.get("count", 20))
    except (TypeError, ValueError):
        return jsonify(error="Song count must be a number."), 400
    if not 1 <= count <= MAX_SONGS:
        return jsonify(error=f"Choose between 1 and {MAX_SONGS} songs."), 400

    try:
        info = sp.artist(artist_id)  # never trust a name sent by the browser
        artist = {"id": info["id"], "name": info["name"]}
        uris = pl.build_playlist(
            sp, artist, count,
            lastfm_key=LASTFM_KEY,
            exclude_variants=data.get("exclude_variants", True) is not False,
            session=lastfm_session,
        )
        if not uris:
            return jsonify(error="No tracks found for that artist."), 404
        url = pl.create_playlist(
            sp, artist["name"], uris, public=data.get("public") is True, name=data.get("name")
        )
    except SpotifyException as e:
        if e.http_status in (400, 404):
            return jsonify(error="That artist could not be found."), 400
        app.logger.warning("Spotify error %s on playlist creation", e.http_status)
        return jsonify(error=f"Spotify error: {e.msg}"), 401 if e.http_status == 401 else 502
    app.logger.info("Playlist created: %s of %s songs", len(uris), count)
    return jsonify(url=url, added=len(uris), requested=count)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
