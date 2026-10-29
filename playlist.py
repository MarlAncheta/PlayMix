"""Core playlist-building logic. No Flask imports, so it can be unit tested on its own."""
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
MAX_POOL = 200
DAMPING = 0.5  # weight ** 0.5: bigger hits are favoured without the top 3 dominating every playlist

SUFFIX_RE = re.compile(r"\s+[-–(\[].*$")  # " - Remastered", " (Live at ...)"
VARIANT_RE = re.compile(r"\b(live|remix|acoustic|demo|instrumental|karaoke)\b", re.I)


class TTLCache:
    """Small thread-safe in-memory cache. Swap for Redis if you run several workers."""

    def __init__(self, ttl=3600):
        self.ttl = ttl
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item and item[0] > time.time():
                return item[1]
            self._data.pop(key, None)
            return None

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time() + self.ttl, value)

    def clear(self):
        with self._lock:
            self._data.clear()


cache = TTLCache()


def lastfm_hits(artist_name, api_key, limit, session=None):
    """Most-played tracks for an artist, with play counts as weights."""
    key = ("lastfm", artist_name.lower(), limit)
    cached = cache.get(key)
    if cached is not None:
        return cached
    r = (session or requests).get(
        LASTFM_URL,
        params={
            "method": "artist.gettoptracks",
            "artist": artist_name,
            "api_key": api_key,
            "format": "json",
            "autocorrect": 1,
            "limit": limit,
        },
        timeout=10,
    )
    r.raise_for_status()
    tracks = r.json().get("toptracks", {}).get("track", [])
    hits = [
        {"title": t["name"], "weight": max(int(t.get("playcount", 1)), 1), "uri": None}
        for t in tracks
    ]
    cache.set(key, hits)
    return hits


def spotify_search_hits(sp, artist, pages=5):
    """Fallback ranking: Spotify search order. Search is capped at 10 results per request."""
    key = ("spotify", artist["id"])
    cached = cache.get(key)
    if cached is not None:
        return cached
    hits, seen = [], set()
    for page in range(pages):
        res = sp.search(q=f'artist:"{artist["name"]}"', type="track", limit=10, offset=page * 10)
        for t in res["tracks"]["items"]:
            title = t["name"].lower()
            if title in seen or not any(a["id"] == artist["id"] for a in t["artists"]):
                continue
            seen.add(title)
            hits.append({"title": t["name"], "weight": 1 / (len(hits) + 1), "uri": t["uri"]})
    cache.set(key, hits)
    return hits


def get_hits(sp, artist, lastfm_key, pool_size, session=None):
    if lastfm_key:
        try:
            hits = lastfm_hits(artist["name"], lastfm_key, pool_size, session)
            if hits:
                return hits
        except (requests.RequestException, ValueError):
            pass  # Last.fm down or unknown artist: fall back to Spotify search
    return spotify_search_hits(sp, artist)


def filter_hits(hits, exclude_variants=True):
    """Drop live/remix/demo versions and collapse duplicates like 'Song' and 'Song - Remastered'."""
    seen, out = set(), []
    for h in hits:
        suffix = SUFFIX_RE.search(h["title"])
        if exclude_variants and suffix and VARIANT_RE.search(suffix.group(0)):
            continue
        key = SUFFIX_RE.sub("", h["title"]).strip().lower() or h["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def weighted_order(hits, rng):
    """Random order where heavier hits tend to come first (Efraimidis-Spirakis sampling)."""
    def key(h):
        w = max(h["weight"], 1e-9) ** DAMPING
        return math.log(rng.random() or 1e-12) / w

    return sorted(hits, key=key, reverse=True)


def resolve_uri(sp, title, artist):
    """Spotify URI for a track title by this artist, or None."""
    key = ("uri", artist["id"], title.lower())
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    clean = title.replace('"', "")
    res = sp.search(q=f'track:"{clean}" artist:"{artist["name"]}"', type="track", limit=10)
    uri = ""
    for t in res["tracks"]["items"]:
        if any(a["id"] == artist["id"] for a in t["artists"]):
            uri = t["uri"]
            break
    cache.set(key, uri)
    return uri or None


def build_playlist(sp, artist, count, lastfm_key=None, exclude_variants=True,
                   rng=None, workers=8, session=None):
    """Return up to `count` unique Spotify URIs, randomly chosen from the artist's biggest hits."""
    rng = rng or random.Random()
    pool_size = min(max(50, count * 2), MAX_POOL)
    hits = filter_hits(get_hits(sp, artist, lastfm_key, pool_size, session), exclude_variants)
    ordered = weighted_order(hits, rng)

    uris, idx = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while len(uris) < count and idx < len(ordered):
            batch = ordered[idx: idx + (count - len(uris))]
            idx += len(batch)
            resolved = pool.map(lambda h: h["uri"] or resolve_uri(sp, h["title"], artist), batch)
            for uri in resolved:
                if uri and uri not in uris:
                    uris.append(uri)
    rng.shuffle(uris)
    return uris


def create_playlist(sp, artist_name, uris, public=False):
    # Spotify removed POST /users/{id}/playlists in 2026; playlists are now created under /me
    playlist = sp._post(
        "me/playlists",
        payload={
            "name": f"{artist_name} - Random Hits",
            "public": public,
            "description": f"{len(uris)} randomly picked hits by {artist_name}",
        },
    )
    for i in range(0, len(uris), 100):  # Spotify accepts at most 100 tracks per request
        # Spotify also renamed /tracks to /items in 2026
        sp._post(f"playlists/{playlist['id']}/items", payload={"uris": uris[i:i + 100]})
    return playlist["external_urls"]["spotify"]
