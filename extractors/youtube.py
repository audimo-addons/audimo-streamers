"""YouTube extractor.

Search uses ytmusicapi (clean music-only results, no API key). Stream
extraction uses yt-dlp against the watch URL. The signed googlevideo
URL yt-dlp returns expires in a few hours, so cache.resolve must
re-extract from the original watch URL on every replay.

Returns **a single best-match result** rather than a list. Reasoning:
ytmusic's `filter="songs"` is already ranked, but raw rank routinely
puts "(Live)" / "(Cover)" / "Karaoke" / sped-up TikTok edits ahead of
the studio version because they have higher recent watch counts.
SoundCloud and Bandcamp keep their multi-result behaviour because
their long tail (remixes, fan uploads) is the point.
"""
from __future__ import annotations

import asyncio
import re
import time


SOURCE_LABEL = "YouTube"

# Tags that almost always mean "not the studio version the user typed."
# We dock candidates whose title contains any of these UNLESS the same
# word appears in the user's query (so a search for "Hey Jude live"
# still returns a live cut).
_VARIANT_TAGS = re.compile(
    r"\b(live|cover|covered|karaoke|instrumental|"
    r"remix|remixed|mashup|sped\s*up|slowed|reverb|"
    r"reaction|tutorial|lesson|piano\s+version|guitar\s+version|"
    r"acoustic\s+(?:cover|version)|8d\s+audio|nightcore)\b",
    re.I,
)


def _query_tags(query: str) -> set[str]:
    """Tokens from the user's query that match _VARIANT_TAGS — these
    are tags they explicitly asked for, so we don't penalise them."""
    return {m.group(0).lower() for m in _VARIANT_TAGS.finditer(query)}


def _score(title: str, query_tags: set[str]) -> int:
    """Rank a candidate. Higher is better. Penalises variant tags the
    user didn't ask for; ties broken by ytmusic's original order."""
    s = 0
    t = (title or "").lower()
    for m in _VARIANT_TAGS.finditer(t):
        if m.group(0).lower() not in query_tags:
            s -= 100
    return s


async def search(query: str, limit: int = 10) -> list[dict]:
    """Pick the single best-match YouTube source for the query.

    The `limit` argument is honoured for the upstream ytmusicapi fetch
    (we look at up to `limit` candidates so the variant-filtering has
    something to choose from), but the return list is always at most
    one entry — the highest-scoring candidate.
    """
    try:
        from ytmusicapi import YTMusic
    except Exception as e:
        print(f"[yt-search] ytmusicapi import failed: {e}", flush=True)
        return []
    try:
        ytmusic = YTMusic()
        # Ask for at least 5 candidates even if the caller passed a
        # smaller limit — we need a real pool to filter from. ytmusic
        # caps at 20 by default; keep it lightweight.
        fetch_n = max(5, min(int(limit) if isinstance(limit, int) else 10, 20))
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ytmusic.search(query, filter="songs", limit=fetch_n)
        )
    except Exception as e:
        print(f"[yt-search] error: {e}", flush=True)
        return []

    if not results:
        return []

    query_tags = _query_tags(query)

    # Build the candidate list, preserving ytmusic's order for ties.
    candidates: list[tuple[int, int, dict]] = []
    for idx, r in enumerate(results):
        video_id = r.get("videoId")
        if not video_id:
            continue
        title = r.get("title", "")
        artists = r.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists) if artists else ""
        thumbs = r.get("thumbnails") or []
        cover = thumbs[-1].get("url", "") if thumbs else ""
        duration = r.get("duration") or ""
        candidates.append((
            _score(title, query_tags),
            -idx,  # negate so ties prefer earlier (higher-ranked) results
            {
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
            },
        ))

    if not candidates:
        return []

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [candidates[0][2]]


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

    base_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
        # yt-dlp defaults to deno only; node is more commonly available
        "js_runtimes": {"node": {}, "bun": {}, "deno": {}},
    }

    # Try extraction with cookies from each browser in order, then
    # without cookies as a last resort (may fail on bot-gated regions).
    #
    # COOKIE SCOPING: each ``cookiesfrombrowser`` entry uses the
    # 4-tuple form ``(browser, profile, keyring, container)`` and
    # passes a youtube-only domain via the `container` slot (yt-dlp
    # interprets matching containers as "this is the only domain we
    # care about"). Without this, yt-dlp loads the entire cookie jar
    # for that browser — gmail, banking, work SSO sessions, anything.
    # The youtube cookies are the only ones googlevideo needs; we
    # don't want the addon process touching the rest, ever.
    _BROWSERS = (
        ("chrome", None, None, "youtube"),
        ("firefox", None, None, "youtube"),
        ("safari", None, None, "youtube"),
        ("edge", None, None, "youtube"),
    )

    # Hard timeout per attempt + overall budget. The previous code
    # could loop through every cookie source if each got blocked by
    # bot detection — extracting from each browser is a 5-10s blocking
    # call, and 4 retries × the no-cookies fallback could spend 60s+
    # before reporting "exhausted". Cap the whole flow at 30s.
    _PER_ATTEMPT_S = 8.0
    _OVERALL_S = 30.0
    _started = time.time()

    async def _try_extract(opts: dict) -> dict | None:
        try:
            return await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: yt_dlp.YoutubeDL(opts).extract_info(watch_url, download=False)
                ),
                timeout=_PER_ATTEMPT_S,
            )
        except asyncio.TimeoutError:
            print(f"[yt-extract] timeout for {watch_url}", flush=True)
            return None  # signal: retry with different cookie source
        except Exception as e:
            msg = str(e)
            if "Sign in" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
                return None  # signal: retry with different cookie source
            print(f"[yt-extract] failed for {watch_url}: {e}", flush=True)
            return False  # signal: real error, stop trying

    info = None
    for cookies in _BROWSERS:
        if time.time() - _started > _OVERALL_S:
            print(f"[yt-extract] overall timeout for {watch_url}", flush=True)
            break
        result = await _try_extract({**base_opts, "cookiesfrombrowser": cookies})
        if result is False:
            return None  # non-recoverable error
        if result is not None:
            info = result
            break

    if info is None and (time.time() - _started) <= _OVERALL_S:
        # Last-ditch attempt with no cookies
        result = await _try_extract(base_opts)
        if not result:
            print(f"[yt-extract] all cookie sources exhausted for {watch_url}", flush=True)
            return None
        info = result
    if info is None:
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
