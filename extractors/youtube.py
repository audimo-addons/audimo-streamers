"""YouTube extractor.

Search uses ytmusicapi (clean music-only results, no API key). Stream
extraction uses yt-dlp against the watch URL. The signed googlevideo
URL yt-dlp returns expires in a few hours, so cache.resolve must
re-extract from the original watch URL on every replay.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


SOURCE_LABEL = "YouTube"


async def search(query: str, limit: int = 10) -> list[dict]:
    """Return ytmusic 'songs' filter results as source records."""
    try:
        from ytmusicapi import YTMusic
    except Exception as e:
        print(f"[yt-search] ytmusicapi import failed: {e}", flush=True)
        return []
    try:
        ytmusic = YTMusic()
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ytmusic.search(query, filter="songs", limit=limit)
        )
    except Exception as e:
        print(f"[yt-search] error: {e}", flush=True)
        return []

    out: list[dict] = []
    for r in results[:limit]:
        video_id = r.get("videoId")
        if not video_id:
            continue
        title = r.get("title", "")
        artists = r.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists) if artists else ""
        thumbs = r.get("thumbnails") or []
        cover = thumbs[-1].get("url", "") if thumbs else ""
        duration = r.get("duration") or ""
        out.append({
            "kind": "youtube",
            "source": SOURCE_LABEL,
            "name": f"{artist} — {title}" if artist else title,
            "title": title,
            "artist": artist,
            "link": f"https://www.youtube.com/watch?v={video_id}",
            "video_id": video_id,
            "duration": duration,
            "albumCover": cover,
            # Resolution is fast (~1s) — flag instant so the picker
            # ranks these alongside debrid-cached torrents.
            "is_cached": True,
        })
    return out


async def extract(watch_url: str) -> dict | None:
    """Run yt-dlp against a watch URL and return stream info.

    Returns ``{"stream_url", "mime_type", "expires_at"}`` or ``None``
    on failure. ``expires_at`` is a unix timestamp; googlevideo URLs
    typically last ~6h.
    """
    try:
        import yt_dlp
    except Exception as e:
        print(f"[yt-extract] yt_dlp import failed: {e}", flush=True)
        return None

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
    }
    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(opts).extract_info(watch_url, download=False)
        )
    except Exception as e:
        print(f"[yt-extract] failed for {watch_url}: {e}", flush=True)
        return None
    stream_url = info.get("url") or ""
    if not stream_url:
        return None
    return {
        "stream_url": stream_url,
        "mime_type": "audio/mpeg",
        # yt-dlp returns expiry in `expires` (unix ts) or in the URL
        # query (`expire=…`). Best-effort: use it if present, else
        # assume 6h. cache.resolve re-extracts from the watch URL
        # anyway, so a stale ts is harmless.
        "expires_at": int(info.get("expires") or (time.time() + 6 * 3600)),
    }
