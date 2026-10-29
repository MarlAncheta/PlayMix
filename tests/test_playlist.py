import random
import re

import pytest
import requests

import playlist as pl

ARTIST = {"id": "a1", "name": "Test Artist"}


@pytest.fixture(autouse=True)
def clear_cache():
    pl.cache.clear()


class FakeSpotify:
    """Stands in for spotipy.Spotify; only resolves titles that are in the catalog."""

    def __init__(self, catalog):
        self.catalog = set(catalog)

    def search(self, q, type, limit, offset=0):
        title = re.search(r'track:"(.*?)"', q).group(1)
        if title in self.catalog:
            return {"tracks": {"items": [{"uri": f"spotify:track:{title}", "artists": [{"id": "a1"}]}]}}
        return {"tracks": {"items": []}}


def hits(*titles):
    return [{"title": t, "weight": 100, "uri": None} for t in titles]


def test_filter_drops_variants_but_keeps_titles_containing_live():
    result = pl.filter_hits(hits("Live and Let Die", "Song - Live at Wembley", "Song (Remix)", "Other"))
    assert [h["title"] for h in result] == ["Live and Let Die", "Other"]


def test_filter_can_keep_variants():
    result = pl.filter_hits(hits("Song", "Song - Live"), exclude_variants=False)
    assert [h["title"] for h in result] == ["Song"]  # still deduped


def test_filter_collapses_duplicate_versions():
    result = pl.filter_hits(hits("Heroes", "Heroes - Remastered 2014", "Heroes (2017 Mix)"))
    assert [h["title"] for h in result] == ["Heroes"]


def test_weighted_order_favours_heavy_tracks():
    rng = random.Random(1)
    pool = [{"title": "big", "weight": 1_000_000}, {"title": "small", "weight": 1}]
    firsts = [pl.weighted_order(pool, rng)[0]["title"] for _ in range(200)]
    assert firsts.count("big") > 180


def test_build_playlist_returns_requested_unique_count(monkeypatch):
    titles = [f"Song {i}" for i in range(30)]
    monkeypatch.setattr(pl, "get_hits", lambda *a, **k: hits(*titles))
    uris = pl.build_playlist(FakeSpotify(titles), ARTIST, 10, rng=random.Random(0))
    assert len(uris) == 10 and len(set(uris)) == 10


def test_build_playlist_skips_tracks_missing_on_spotify(monkeypatch):
    titles = [f"Song {i}" for i in range(20)]
    monkeypatch.setattr(pl, "get_hits", lambda *a, **k: hits(*titles))
    sp = FakeSpotify(titles[::2])  # only half can be found
    uris = pl.build_playlist(sp, ARTIST, 8, rng=random.Random(0))
    assert len(uris) == 8


def test_build_playlist_returns_fewer_when_not_enough_tracks(monkeypatch):
    monkeypatch.setattr(pl, "get_hits", lambda *a, **k: hits("A", "B", "C"))
    uris = pl.build_playlist(FakeSpotify("ABC"), ARTIST, 10, rng=random.Random(0))
    assert len(uris) == 3


def test_get_hits_falls_back_to_spotify_when_lastfm_fails(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    fallback = hits("From Spotify")
    monkeypatch.setattr(pl, "lastfm_hits", boom)
    monkeypatch.setattr(pl, "spotify_search_hits", lambda *a, **k: fallback)
    assert pl.get_hits(None, ARTIST, "key", 50) == fallback


def test_resolved_uris_are_cached():
    calls = []

    class Counting(FakeSpotify):
        def search(self, *a, **k):
            calls.append(1)
            return super().search(*a, **k)

    sp = Counting(["Song"])
    pl.resolve_uri(sp, "Song", ARTIST)
    pl.resolve_uri(sp, "Song", ARTIST)
    assert len(calls) == 1


def test_create_playlist_uses_2026_endpoints():
    calls = []

    class Recorder:
        def _post(self, url, payload=None):
            calls.append((url, payload))
            return {"id": "p1", "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"}}

    uris = [f"spotify:track:{i}" for i in range(150)]
    link = pl.create_playlist(Recorder(), "Test Artist", uris, public=True)

    assert link == "https://open.spotify.com/playlist/p1"
    assert calls[0][0] == "me/playlists" and calls[0][1]["public"] is True
    assert [c[0] for c in calls[1:]] == ["playlists/p1/items", "playlists/p1/items"]
    assert [len(c[1]["uris"]) for c in calls[1:]] == [100, 50]  # batched at 100


DEFAULT = "Test Artist - Random Hits"


@pytest.mark.parametrize("raw, expected", [
    (None, DEFAULT),
    ("", DEFAULT),
    ("   ", DEFAULT),
    (123, DEFAULT),
    ("My road trip mix", "My road trip mix"),
    ("  spaced    out  ", "spaced out"),
    ("line\nbreak\tand\x00null", "line break and null"),
    ("x" * 300, "x" * 100),
])
def test_clean_playlist_name(raw, expected):
    assert pl.clean_playlist_name(raw, DEFAULT) == expected


def test_create_playlist_uses_custom_name_or_default():
    sent = []

    class Recorder:
        def _post(self, url, payload=None):
            sent.append(payload)
            return {"id": "p1", "external_urls": {"spotify": "https://x"}}

    pl.create_playlist(Recorder(), "Test Artist", ["u1"], name="Friday vibes")
    pl.create_playlist(Recorder(), "Test Artist", ["u1"], name="  ")
    pl.create_playlist(Recorder(), "Test Artist", ["u1"])
    names = [p["name"] for p in sent if "name" in p]
    assert names == ["Friday vibes", DEFAULT, DEFAULT]
