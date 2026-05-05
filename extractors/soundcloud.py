"""SoundCloud extractor.

Ports the implementation from the native app's backend/main.py. We
scrape a client_id out of soundcloud.com's JS bundle (rotates every
few weeks; SC returns 401 when our cached value goes stale, which is
how we know to refetch). The api-v2 search endpoint then returns
streamable tracks; per-track media transcodings give us the actual
mp3/HLS URL.

Subscription-only ("SNIP" policy) tracks are dropped at search time
so the user never picks a 30-second preview as if it were the song.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx


SC_BASE = "https://api-v2.soundcloud.com"
SOURCE_LABEL = "SoundCloud"

_STATE_DIR = Path(os.environ.get("AUDIMO_STREAMERS_STATE_DIR") or
                  os.path.expanduser("~/.audimo-streamers"))
_CLIENT_ID_FILE = _STATE_DIR / "sc_client_id.json"
_CLIENT_ID_TTL_S = 24 * 60 * 60

_cached_id: str | None = None
_id_lock = asyncio.Lock()


def _load_disk() -> str | None:
    try:
        data = json.loads(_CLIENT_ID_FILE.read_text())
        cid = (data.get("id") or "").strip()
        ts = float(data.get("fetched_at") or 0)
        if not cid or (time.time() - ts) > _CLIENT_ID_TTL_S:
            return None
        return cid
    except (FileNotFoundError, ValueError, OSError):
        return None


def _save_disk(client_id: str) -> None:
    try:
        _CLIENT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CLIENT_ID_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": client_id, "fetched_at": time.time()}))
        os.replace(tmp, _CLIENT_ID_FILE)
    except OSError as e:
        print(f"[sc] persist client_id failed: {e}", flush=True)


def invalidate_client_id() -> None:
    """Drop the cached client_id; next call will rescrape."""
    global _cached_id
    _cached_id = None
    try:
        _CLIENT_ID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def _get_client_id() -> str | None:
    global _cached_id
    if _cached_id:
        return _cached_id
    cached = _load_disk()
    if cached:
        _cached_id = cached
        return cached
    async with _id_lock:
        if _cached_id:
            return _cached_id
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(
                    "https://soundcloud.com",
                    headers={"User-Agent":
                             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36"},
                )
                js_urls = re.findall(r'https://a-v2\.sndcdn\.com/assets/[^"]+\.js', r.text)
                for js_url in js_urls[-5:]:
                    jr = await client.get(js_url)
                    m = re.search(r'client_id:"([a-zA-Z0-9]{32})"', jr.text)
                    if m:
                        _cached_id = m.group(1)
                        _save_disk(_cached_id)
                        print(f"[sc] got client_id {_cached_id[:8]}…", flush=True)
                        return _cached_id
        except Exception as e:
            print(f"[sc] scrape failed: {e}", flush=True)
    return None


async def search(query: str, limit: int = 10) -> list[dict]:
    client_id = await _get_client_id()
    if not client_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SC_BASE}/search/tracks",
                params={"q": query, "client_id": client_id,
                        "limit": limit, "offset": 0},
            )
            if r.status_code in (401, 403):
                invalidate_client_id()
                return []
            if r.status_code != 200:
                return []
            data = r.json()
            out: list[dict] = []
            for item in data.get("collection", []):
                if not item.get("streamable"):
                    continue
                if (item.get("policy") or "").upper() == "SNIP":
                    continue
                stream_url = await _resolve_stream_url(client, item, client_id)
                if not stream_url:
                    continue
                out.append({
                    "kind": "soundcloud",
                    "source": SOURCE_LABEL,
                    "name": f"{(item.get('user') or {}).get('username','')} — "
                            f"{item.get('title','')}",
                    "title": item.get("title", ""),
                    "artist": (item.get("user") or {}).get("username", ""),
                    "link": item.get("permalink_url", ""),
                    "track_id": str(item.get("id") or ""),
                    "duration": (item.get("duration") or 0) // 1000,
                    "albumCover": (item.get("artwork_url") or "")
                        .replace("-large", "-t300x300") or None,
                    "playback_count": item.get("playback_count") or 0,
                    "is_cached": True,
                    # Stash the resolved stream so resolve.stream
                    # can hand it back without a second scrape.
                    "_stream_url": stream_url,
                })
            return out
    except Exception as e:
        print(f"[sc] search error: {e}", flush=True)
        return []


async def _resolve_stream_url(client: httpx.AsyncClient, item: dict,
                              client_id: str) -> str | None:
    """Pick a non-snippet transcoding (progressive > HLS) and resolve it."""
    try:
        media = (item.get("media") or {}).get("transcodings") or []
        full = [t for t in media if not t.get("snipped")]
        progressive = [t for t in full
                       if (t.get("format") or {}).get("protocol") == "progressive"]
        hls = [t for t in full
               if (t.get("format") or {}).get("protocol") == "hls"]
        for transcoding in (progressive or hls)[:2]:
            url = transcoding.get("url")
            if not url:
                continue
            r = await client.get(url, params={"client_id": client_id}, timeout=8)
            if r.status_code == 200:
                stream_url = r.json().get("url")
                if stream_url:
                    return stream_url
    except Exception as e:
        print(f"[sc] transcoding resolve failed: {e}", flush=True)
    return None


async def extract(permalink_url: str) -> dict | None:
    """Resolve a SoundCloud track URL to a fresh stream URL.

    Used by cache.resolve when a previously-played track's URL has
    expired. We re-resolve via the public ``/resolve`` endpoint so we
    always get a current transcoding pointer.
    """
    client_id = await _get_client_id()
    if not client_id or not permalink_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SC_BASE}/resolve",
                params={"url": permalink_url, "client_id": client_id},
            )
            if r.status_code in (401, 403):
                invalidate_client_id()
                return None
            if r.status_code != 200:
                return None
            item = r.json()
            stream_url = await _resolve_stream_url(client, item, client_id)
            if not stream_url:
                return None
            return {
                "stream_url": stream_url,
                "mime_type": "audio/mpeg",
                # SC stream URLs are short-lived — re-extract every
                # play to be safe. 1h is conservative.
                "expires_at": int(time.time() + 3600),
            }
    except Exception as e:
        print(f"[sc] extract failed: {e}", flush=True)
        return None
