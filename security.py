"""Security helpers: encrypted token storage, response headers, origin checks, config validation."""
import base64
import hashlib
import json
import secrets
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from flask import g, jsonify, request
from spotipy.cache_handler import CacheHandler

REQUIRED_PRODUCTION_VARS = (
    "FLASK_SECRET_KEY",
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
    "SPOTIPY_REDIRECT_URI",
)


def check_production_config(env):
    """Refuse to start in production with missing or unsafe settings."""
    missing = [name for name in REQUIRED_PRODUCTION_VARS if not env.get(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    if len(env["FLASK_SECRET_KEY"]) < 32:
        raise RuntimeError("FLASK_SECRET_KEY must be at least 32 characters long.")
    if not env["SPOTIPY_REDIRECT_URI"].startswith("https://"):
        raise RuntimeError("SPOTIPY_REDIRECT_URI must start with https:// in production.")


def make_fernet(secret_key):
    """Derive an encryption key from the app secret, so no extra setting is needed."""
    digest = hashlib.sha256(b"spotify-token-encryption:" + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class EncryptedSessionCacheHandler(CacheHandler):
    """Stores the Spotify token in the session cookie, encrypted, so the cookie is unreadable."""

    def __init__(self, session, fernet, key="token_info"):
        self.session = session
        self.fernet = fernet
        self.key = key

    def get_cached_token(self):
        blob = self.session.get(self.key)
        if not blob:
            return None
        try:
            return json.loads(self.fernet.decrypt(blob.encode()).decode())
        except (InvalidToken, ValueError):
            return None  # tampered, or encrypted with an old key: treat as logged out

    def save_token_to_cache(self, token_info):
        self.session[self.key] = self.fernet.encrypt(json.dumps(token_info).encode()).decode()


def block_cross_site_requests():
    """Reject state-changing requests that did not come from this site (CSRF defence in depth)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin:
        same_site = urlparse(origin).netloc == request.host
    else:
        same_site = request.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
    if not same_site:
        return jsonify(error="Cross-site request blocked."), 403
    return None


def init_security(app, production):
    @app.before_request
    def _prepare():
        g.csp_nonce = secrets.token_urlsafe(16)
        return block_cross_site_requests()

    @app.context_processor
    def _inject_nonce():
        return {"csp_nonce": g.get("csp_nonce", "")}

    @app.after_request
    def _add_headers(resp):
        nonce = g.get("csp_nonce", "")
        resp.headers["Content-Security-Policy"] = "; ".join([
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com",
            "font-src https://fonts.gstatic.com",
            "img-src 'self' https://*.scdn.co data:",  # Spotify artist images
            "connect-src 'self'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ])
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.path == "/" or request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
