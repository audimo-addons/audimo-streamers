"""Bandcamp extractor.

Search via Bandcamp's internal autocomplete_elastic JSON endpoint
(no API key, no auth). Stream extraction by parsing the
``data-tralbum`` blob embedded in each track page — that's how the
public web player gets its mp3-128 URL, so anonymous visitors can
hit it.

Tracks gated behind a purchase have no public stream URL. We omit
those at extract time rather than emitting an "unsupported" event.
"""
from __future__ import annotations

import html
import json
import re
import time

import httpx


SOURCE_LABEL = "Bandcamp"

_HEADERS = {
    "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
}


async def search(query: str, limit: int = 12) -> list[dict]:
    url = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
    payload = {
        "search_text": query,
        "search_filter": "t",   # tracks only
        "full_page": False,
        "fan_id": None,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=_HEADERS)
            if r.status_code != 200:
                return []
            data = r.json()
            results = (data.get("auto") or {}).get("results", [])
            out: list[dict] = []
            for item in results[:limit]:
                if item.get("type") != "t":
                    continue
                track_url = (item.get("item_url_path") or
                             item.get("item_url_root") or "")
                if not track_url:
                    continue
                cover = item.get("img") or None
                art_id = item.get("art_id")
                if art_id:
                    cover = f"https://f4.bcbits.com/img/a{int(art_id):010d}_9.jpg"
                out.append({
                    "kind": "bandcamp",
                    "source": SOURCE_LABEL,
                    "name": f"{item.get('band_name','')} — {item.get('name','')}",
                    "title": item.get("name") or "",
                    "artist": item.get("band_name") or "",
                    "album": item.get("album_name") or "",
                    "albumCover": cover,
                    "link": track_url,
                    # Bandcamp resolution requires fetching the track
                    # page so it's slower than YT/SC — flag is_cached
                    # anyway because the user perceives it as instant
                    # compared to torrent peering.
                    "is_cached": True,
                })
            return out
    except Exception as e:
        print(f"[bc] search error: {e}", flush=True)
        return []


async def extract(track_url: str) -> dict | None:
    """Pull mp3-128 stream URL from a Bandcamp track page.

    Returns None for tracks gated behind purchase (no public stream).
    """
    if not track_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(
                track_url,
                headers={"User-Agent": _HEADERS["User-Agent"]},
            )
            if r.status_code != 200:
                return None
            m = re.search(r'data-tralbum="([^"]+)"', r.text)
            if not m:
                return None
            blob = html.unescape(m.group(1))
            try:
                tralbum = json.loads(blob)
            except Exception:
                return None
            trackinfo = tralbum.get("trackinfo") or []
            if not trackinfo:
                return None
            first = trackinfo[0]
            stream_url = (first.get("file") or {}).get("mp3-128")
            if not stream_url:
                return None
            if stream_url.startswith("//"):
                stream_url = "https:" + stream_url
            return {
                "stream_url": stream_url,
                "mime_type": "audio/mpeg",
                # Bandcamp stream URLs aren't strictly time-bound but
                # we re-extract on each play anyway. 12h is plenty.
                "expires_at": int(time.time() + 12 * 3600),
            }
    except Exception as e:
        print(f"[bc] extract failed: {e}", flush=True)
        return None
