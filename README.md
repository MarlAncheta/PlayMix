# Hit Shuffle

A Flask web app that builds a Spotify playlist from an artist's biggest songs. Pick an artist, choose how many songs (1 to 100), and the app saves a playlist of randomly chosen hits to your Spotify account.

## How it works

- **Spotify OAuth 2.0 (authorization code flow)** with a CSRF `state` check. Tokens are stored per user in a signed session cookie and refreshed automatically.
- **Hit ranking:** Spotify removed its "artist top tracks" endpoint for new apps in 2026, so play counts come from the Last.fm API. If Last.fm is unavailable or no key is set, the app falls back to Spotify search order.
- **Weighted random selection:** bigger hits are more likely to be picked, with dampening so the top few songs don't appear in every playlist.
- **Clean-up:** optionally skips live, remix, acoustic, demo and karaoke versions, and collapses duplicates such as remasters.
- **Speed:** track lookups run concurrently in a thread pool, and Last.fm and Spotify lookups are cached in memory with a TTL.
- **Resilience:** automatic retry with backoff on Spotify rate limits, input validation, and clear error messages in the UI.

## Run it locally

1. Create an app at https://developer.spotify.com/dashboard and add `http://127.0.0.1:5000/callback` as a redirect URI. Spotify requires a Premium account for app owners.
2. Get a free Last.fm API key at https://www.last.fm/api.
3. Copy `.env.example` to `.env` and fill it in.
4. Install and run:

```bash
pip install -r requirements-dev.txt
python app.py
```

Open http://127.0.0.1:5000 (use `127.0.0.1`, not `localhost`, so the session cookie matches the redirect URI).

## Tests

```bash
pytest -q
```

Tests mock Spotify and Last.fm, and run on every push through GitHub Actions.

## Deploy

Set the environment variables from `.env.example` on your host (plus `PRODUCTION=1` for secure cookies), add your deployed `/callback` URL to the Spotify dashboard, and start with:

```bash
gunicorn app:app
```

With more than one worker, replace the in-memory cache in `playlist.py` with Redis.

## Limitations

Spotify development mode allows only 5 authorized users per app, and search returns at most 10 results per request.
