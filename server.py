"""audimo_streamers — public-web streaming addon.

Wraps the three free public sources Audimo previously had baked into
its native backend — YouTube, SoundCloud, Bandcamp — into the standard
addon protocol. Each source returned is stamped
``addon_id = "audimo-streamers"`` so the AIO aggregator routes
follow-up resolve.stream / cache.resolve calls back here.

Capabilities:
    resolve.sources         JSON fan-out across enabled extractors
    resolve.sources.stream  SSE variant (one section per extractor)
    resolve.stream          SSE — extracts a fresh stream URL
    cache.resolve           re-extract from the original page URL
    search.tracks           thin wrapper over resolve.sources

This addon is **public-host-safe**: no debrid, no torrent, no library
writes. The only state on disk is a SoundCloud client_id cache (to
avoid rescraping on every search).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.parse
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from extractors import bandcamp, soundcloud, youtube


# ── Manifest ────────────────────────────────────────────────────────

HOSTED = (os.environ.get("AUDIMO_HOSTED") or "").strip().lower() in ("1", "true", "yes")

MANIFEST = {
    "id": "audimo-streamers",
    "name": "Audimo Streamers" + (" (hosted)" if HOSTED else ""),
    "version": "0.1.2",
    "description": (
        "Plays YouTube, SoundCloud, and Bandcamp as Audimo sources. "
        "Free public web streams, no account needed. Each service can "
        "be toggled individually."
    ),
    "capabilities": [
        "resolve.sources",
        "resolve.sources.stream",
        "resolve.stream",
        "cache.resolve",
        "search.tracks",
    ],
    "display": {
        "label": "Web streamers",
        "icon": "",
    },
    "settings_schema": [
        {
            "type": "section",
            "label": "Hosted access",
            "description":
                "If you're running this addon on a public URL, set "
                "AUDIMO_ADDON_KEY on the server and paste the same value "
                "here. Leave blank for local installs.",
            "fields": [
                {
                    "key": "addon_key",
                    "type": "password",
                    "label": "Addon access key",
                    "description":
                        "Shared secret with the server's "
                        "AUDIMO_ADDON_KEY env var.",
                    "placeholder": "long-random-string",
                },
            ],
        },
        {
            "type": "section",
            "label": "Sources",
            "description": "Toggle each public source individually.",
            "fields": [
                {
                    "key": "enable_youtube",
                    "type": "boolean",
                    "label": "YouTube",
                    "description":
                        "Search via YouTube Music; extract streams via "
                        "yt-dlp. Stream URLs expire every ~6h, so the "
                        "addon re-extracts on each play.",
                    "default": True,
                },
                {
                    "key": "enable_soundcloud",
                    "type": "boolean",
                    "label": "SoundCloud",
                    "description":
                        "Public free tracks only. SoundCloud Go+ "
                        "subscription tracks are skipped.",
                    "default": True,
                },
                {
                    "key": "enable_bandcamp",
                    "type": "boolean",
                    "label": "Bandcamp",
                    "description":
                        "Public free streams + previews. Tracks gated "
                        "behind purchase are not playable.",
                    "default": True,
                },
            ],
        },
        {
            "type": "section",
            "label": "Limits",
            "fields": [
                {
                    "key": "limit_per_source",
                    "type": "number",
                    "label": "Max results per source",
                    "description":
                        "Caps how many sources each enabled service "
                        "returns. Combined limit is ~3× this.",
                    "default": 10,
                },
            ],
        },
    ],
}


# ── App + middleware ────────────────────────────────────────────────

app = FastAPI(
    title="audimo-streamers",
    version=MANIFEST["version"],
    docs_url="/docs" if str(os.environ.get("AUDIMO_DEBUG", "")).lower() in {"1", "true", "yes"} else None,
    redoc_url=None,
    openapi_url="/openapi.json" if str(os.environ.get("AUDIMO_DEBUG", "")).lower() in {"1", "true", "yes"} else None,
)

# Browser-direct addon calls are first-class — the orchestrator runs
# in the user's browser and talks to the addon directly. CORS = "*"
# is required for the cross-origin GET of /manifest.json and the
# POSTs that follow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_ADDON_KEY = (os.environ.get("AUDIMO_ADDON_KEY") or "").strip()


@app.middleware("http")
async def _require_addon_key(request: Request, call_next):
    """Optional shared-secret gate — the same pattern audimo-indexers
    uses. Manifest, /configure, /health stay public so install URLs
    and configure pages still load when an addon key is in effect."""
    if not _ADDON_KEY:
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    p = request.url.path
    if p.endswith("/manifest.json") or p.endswith("/configure") or p == "/health":
        return await call_next(request)
    presented = request.headers.get("x-audimo-addon-key", "").strip()
    if not presented or presented != _ADDON_KEY:
        return JSONResponse({"detail": "addon key required"}, status_code=401)
    return await call_next(request)


# ── Config decoding ─────────────────────────────────────────────────

def _parse_config_str(s: str) -> dict:
    """Decode the path-segmented config blob (Stremio-style).

    Tries base64url-encoded JSON first, falls back to URL-encoded JSON.
    Returns {} on any decode error so a malformed install URL never
    500s — caller falls back to body settings + manifest defaults.
    """
    if not s:
        return {}
    try:
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
        except Exception:
            raw = urllib.parse.unquote(s)
        return json.loads(raw)
    except Exception:
        return {}


def _config_from(request: Request, payload: dict | None = None) -> dict:
    body_cfg = dict((payload or {}).get("settings") or {}) if payload else {}
    raw = request.path_params.get("config", "") or ""
    path_cfg = _parse_config_str(raw) if raw else {}
    # Path config wins — install URLs may carry an addon_key that
    # overrides whatever the body sends.
    return {**body_cfg, **path_cfg}


def _bool(cfg: dict, key: str, default: bool = False) -> bool:
    v = cfg.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _int(cfg: dict, key: str, default: int) -> int:
    v = cfg.get(key)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(50, n))


# ── Source dispatch ─────────────────────────────────────────────────

# Each entry: (config-key, kind-tag, search-callable, extract-callable)
# kind-tag matches the `kind` field stamped onto each source record so
# resolve.stream knows which extractor to call.
EXTRACTORS = [
    ("enable_youtube",   "youtube",    youtube.search,    youtube.extract),
    ("enable_soundcloud", "soundcloud", soundcloud.search, soundcloud.extract),
    ("enable_bandcamp",  "bandcamp",   bandcamp.search,   bandcamp.extract),
]


def _enabled_extractors(cfg: dict):
    out = []
    for cfg_key, kind, search_fn, extract_fn in EXTRACTORS:
        if _bool(cfg, cfg_key, default=True):
            out.append((kind, search_fn, extract_fn))
    return out


def _stamp(sources: list[dict]) -> list[dict]:
    """Stamp every source with the addon_id so the aggregator can route
    follow-up calls back here. Idempotent if already stamped."""
    for s in sources:
        s["addon_id"] = MANIFEST["id"]
    return sources


def _build_query(payload: dict) -> str:
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    return (f"{artist} {title}" if artist else title).strip()


# ── HTTP routes ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return f"""
    <!doctype html><meta charset="utf-8">
    <title>audimo-streamers</title>
    <body style="background:#111;color:#ddd;font-family:system-ui;padding:24px">
      <h1>audimo-streamers v{MANIFEST['version']}</h1>
      <p>YouTube · SoundCloud · Bandcamp</p>
      <p><a href="/configure" style="color:#7af">/configure</a> ·
         <a href="/manifest.json" style="color:#7af">/manifest.json</a> ·
         <a href="/health" style="color:#7af">/health</a></p>
    </body>"""


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": MANIFEST["version"]}


@app.get("/version")
async def version() -> dict:
    return {"version": MANIFEST["version"]}


def _public_manifest() -> dict:
    return {**MANIFEST, "hosted": HOSTED}


@app.get("/manifest.json")
@app.get("/{config}/manifest.json")
async def manifest(config: str = "") -> dict:
    return _public_manifest()


# ── /resolve/sources (JSON) ─────────────────────────────────────────

@app.post("/resolve/sources")
@app.post("/{config}/resolve/sources")
async def resolve_sources(payload: dict, request: Request, config: str = "") -> dict:
    """Fan out across enabled extractors in parallel; return a combined
    sources list stamped with addon_id."""
    query = _build_query(payload)
    if not query:
        raise HTTPException(400, "title (and optionally artist) required")

    cfg = _config_from(request, payload)
    extractors = _enabled_extractors(cfg)
    if not extractors:
        return {"sources": []}

    limit = _int(cfg, "limit_per_source", 10)
    results = await asyncio.gather(
        *(search_fn(query, limit) for _, search_fn, _ in extractors),
        return_exceptions=True,
    )

    sources: list[dict] = []
    for (kind, _, _), res in zip(extractors, results):
        if isinstance(res, BaseException):
            print(f"[sources] {kind} raised: "
                  f"{type(res).__name__}: {str(res)[:200]}", flush=True)
            continue
        sources.extend(res or [])

    return {"sources": _stamp(sources)}


# ── /resolve/sources/stream (SSE) ───────────────────────────────────

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/resolve/sources/stream")
@app.post("/{config}/resolve/sources/stream")
async def resolve_sources_stream(payload: dict, request: Request,
                                 config: str = "") -> StreamingResponse:
    """SSE variant — emits one section per extractor as its results
    arrive, then a final ``done``."""
    query = _build_query(payload)
    if not query:
        async def err() -> AsyncGenerator[str, None]:
            yield _sse({"type": "error", "code": "missing_title",
                        "message": "title (and optionally artist) required"})
            yield _sse({"type": "done"})
        return StreamingResponse(err(), media_type="text/event-stream")

    cfg = _config_from(request, payload)
    extractors = _enabled_extractors(cfg)
    limit = _int(cfg, "limit_per_source", 10)

    async def stream() -> AsyncGenerator[str, None]:
        if not extractors:
            yield _sse({"type": "done"})
            return

        # Each extractor is a coroutine — wrap with a label so we know
        # which finished as gather completes.
        async def _run(label: str, coro):
            try:
                return label, await coro
            except Exception as e:
                return label, e

        pending = {
            asyncio.create_task(_run(kind, search_fn(query, limit)))
            for kind, search_fn, _ in extractors
        }

        # Map kind → human label for section emission.
        labels = {
            "youtube": "YouTube",
            "soundcloud": "SoundCloud",
            "bandcamp": "Bandcamp",
        }

        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                kind, result = task.result()
                if isinstance(result, BaseException):
                    yield _sse({"type": "progress",
                                "message": f"{labels.get(kind, kind)} failed",
                                "error": str(result)[:200]})
                    continue
                sources = _stamp(result or [])
                yield _sse({
                    "type": "section",
                    "label": labels.get(kind, kind),
                    "icon": "",
                    "sources": sources,
                })
        yield _sse({"type": "done"})

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── /resolve/stream (SSE) ───────────────────────────────────────────

def _extractor_for_kind(kind: str):
    for _, k, search_fn, extract_fn in EXTRACTORS:
        if k == kind:
            return extract_fn
    return None


@app.post("/resolve/stream")
@app.post("/{config}/resolve/stream")
async def resolve_stream(payload: dict, request: Request,
                         config: str = "") -> StreamingResponse:
    """Resolve a chosen source to a fresh playable URL.

    The source dict comes back from /resolve/sources verbatim, with our
    ``kind`` field telling us which extractor owns it.
    """
    source = payload.get("source") or {}
    kind = (source.get("kind") or "").lower()
    link = (source.get("link") or "").strip()

    # SoundCloud sources stash the resolved transcoding URL in
    # ``_stream_url`` at search time so the picker shows instant
    # results without a second scrape. Honor that fast path.
    sc_cached = (source.get("_stream_url") or "").strip()

    extract_fn = _extractor_for_kind(kind)
    if not extract_fn:
        async def err() -> AsyncGenerator[str, None]:
            yield _sse({"type": "error",
                        "code": "unsupported_source_kind",
                        "message": f"this addon does not handle kind={kind!r}"})
        return StreamingResponse(err(), media_type="text/event-stream")

    if not link and not sc_cached:
        async def err() -> AsyncGenerator[str, None]:
            yield _sse({"type": "error",
                        "code": "missing_link",
                        "message": "source.link required"})
        return StreamingResponse(err(), media_type="text/event-stream")

    label = (source.get("source") or kind).title()

    async def stream() -> AsyncGenerator[str, None]:
        yield _sse({
            "type": "progress",
            "pct": 5,
            "message": f"Resolving {label}…",
        })

        # SC fast path — already resolved at search time.
        if kind == "soundcloud" and sc_cached:
            yield _sse({
                "type": "ready",
                "stream_url": sc_cached,
                "mime_type": "audio/mpeg",
                "source_label": label,
                "pct": 100,
            })
            return

        result = await extract_fn(link)
        if not result:
            yield _sse({
                "type": "error",
                "code": "extract_failed",
                "message": f"Could not extract a stream URL from {label}.",
            })
            return

        yield _sse({
            "type": "ready",
            "stream_url": result.get("stream_url", ""),
            "mime_type": result.get("mime_type", "audio/mpeg"),
            "source_label": label,
            "expires_at": result.get("expires_at"),
            "pct": 100,
        })

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── /cache/resolve (JSON) ───────────────────────────────────────────

@app.post("/cache/resolve")
@app.post("/{config}/cache/resolve")
async def cache_resolve(payload: dict, request: Request,
                        config: str = "") -> dict:
    """Re-extract a stored entry's stream URL.

    Library rows the SourcePicker writes for addon-resolved tracks
    nest the addon's source under ``entry.source_payload`` and stamp
    ``entry.type='addon'`` at the top level. So our `kind` (youtube /
    soundcloud / bandcamp) and the original page URL live in
    ``source_payload.{kind,link}``, NOT at the entry root.

    We look there first, then fall back to top-level fields for older
    library rows / hand-built entries.
    """
    entry = payload.get("entry") or {}
    sp = entry.get("source_payload") or {}

    kind = ((sp.get("kind")
             or entry.get("kind")
             # entry.type is "addon" for SourcePicker-saved rows; only
             # honour it as a kind hint for legacy non-addon entries.
             or (entry.get("type") if entry.get("type") in ("youtube", "soundcloud", "bandcamp") else "")
             or "").lower().strip())

    page_url = ((sp.get("link") or sp.get("permalink") or sp.get("source_url")
                 or entry.get("source_url") or entry.get("link")
                 or entry.get("permalink")
                 # streamUrl is the **expired** googlevideo URL for YT
                 # rows — useless for re-extraction. Skip it. Bandcamp
                 # / SoundCloud streamUrls are also expired by the time
                 # cache.resolve runs.
                 or "").strip())

    extract_fn = _extractor_for_kind(kind)
    if not extract_fn:
        return {"error": "unresolvable",
                "message": f"no extractor for kind={kind!r}"}
    if not page_url:
        return {"error": "unresolvable",
                "message": f"no source URL on entry (looked in source_payload.link, entry.link)"}

    result = await extract_fn(page_url)
    if not result:
        return {"error": "extract_failed"}

    # Don't .title() a label we already have — that lowercases
    # "YouTube" to "Youtube". Only title-case the kind fallback.
    label = sp.get("source") or entry.get("source") or kind.title()
    return {
        "streamUrl": result.get("stream_url", ""),
        "mimeType": result.get("mime_type", "audio/mpeg"),
        "source": label,
        "expires_at": result.get("expires_at"),
        "albumCover": entry.get("albumCover") or sp.get("albumCover"),
        "filename": entry.get("filename", ""),
    }


# ── /search/tracks (JSON, optional convenience) ─────────────────────

@app.post("/search/tracks")
@app.post("/{config}/search/tracks")
async def search_tracks(payload: dict, request: Request,
                        config: str = "") -> dict:
    """Thin wrapper over /resolve/sources for clients that just want a
    flat track listing (no per-track resolve flow). Same shape, same
    addon_id stamping."""
    return await resolve_sources(payload, request, config)


# ── /configure (HTML) ───────────────────────────────────────────────

@app.get("/configure", response_class=HTMLResponse)
@app.get("/{config}/configure", response_class=HTMLResponse)
async def configure(request: Request, config: str = "") -> str:
    """Minimal install-URL builder. Mirrors the indexers /configure
    pattern: pure client-side JS that base64url-encodes the settings
    blob and posts it to the parent window via postMessage so
    Audimo's Addons tab can register the resulting URL with one
    click. Falls back to a copy-pasteable URL for manual install."""
    existing = _parse_config_str(config) if config else {}
    return f"""
    <!doctype html><meta charset="utf-8">
    <title>Configure Audimo Streamers</title>
    <body style="background:#111;color:#eee;font-family:system-ui;padding:24px;max-width:560px;margin:auto">
      <h1>Audimo Streamers</h1>
      <p style="color:#aaa">Pick which public web sources to expose, then click
      <b>Install</b> to add this addon to Audimo.</p>

      <fieldset style="border:1px solid #333;padding:12px;margin:16px 0">
        <legend style="color:#aaa">Sources</legend>
        <label style="display:block;margin:8px 0">
          <input type="checkbox" id="enable_youtube"
            {'checked' if _bool(existing, 'enable_youtube', True) else ''}>
          YouTube</label>
        <label style="display:block;margin:8px 0">
          <input type="checkbox" id="enable_soundcloud"
            {'checked' if _bool(existing, 'enable_soundcloud', True) else ''}>
          SoundCloud</label>
        <label style="display:block;margin:8px 0">
          <input type="checkbox" id="enable_bandcamp"
            {'checked' if _bool(existing, 'enable_bandcamp', True) else ''}>
          Bandcamp</label>
      </fieldset>

      <fieldset style="border:1px solid #333;padding:12px;margin:16px 0">
        <legend style="color:#aaa">Hosted access (optional)</legend>
        <label style="display:block;color:#aaa;margin:8px 0">
          Addon access key (only required if the host set AUDIMO_ADDON_KEY)
          <input type="password" id="addon_key" style="width:100%;padding:6px;background:#222;color:#eee;border:1px solid #333"
            value="{(existing.get('addon_key') or '')!s}">
        </label>
      </fieldset>

      <button id="install" style="background:#7af;color:#000;padding:10px 16px;border:0;border-radius:4px;cursor:pointer;font-weight:600">
        Install in Audimo
      </button>
      <pre id="url" style="background:#000;color:#7af;padding:10px;margin-top:16px;overflow-x:auto;border-radius:4px"></pre>

      <script>
        function buildCfg() {{
          const cfg = {{
            enable_youtube: document.getElementById('enable_youtube').checked,
            enable_soundcloud: document.getElementById('enable_soundcloud').checked,
            enable_bandcamp: document.getElementById('enable_bandcamp').checked,
          }};
          const k = document.getElementById('addon_key').value.trim();
          if (k) cfg.addon_key = k;
          return cfg;
        }}
        function buildUrl() {{
          const cfg = buildCfg();
          const json = JSON.stringify(cfg);
          const b64 = btoa(json).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
          return location.origin + '/' + b64 + '/manifest.json';
        }}
        const urlBox = document.getElementById('url');
        function refresh() {{ urlBox.textContent = buildUrl(); }}
        document.querySelectorAll('input').forEach(el =>
          el.addEventListener('input', refresh));
        refresh();
        document.getElementById('install').addEventListener('click', () => {{
          const url = buildUrl();
          // Audimo's Addons tab listens for this postMessage and
          // auto-installs the URL. Same protocol the indexers addon
          // uses ({{ type: 'tunnel-addon:install', addonId, url }} —
          // legacy 'tunnel' string preserved for back-compat with
          // existing frontend builds).
          if (window.opener) {{
            window.opener.postMessage({{
              type: 'tunnel-addon:install',
              addonId: 'audimo-streamers',
              url,
            }}, '*');
            window.close();
          }} else if (window.parent && window.parent !== window) {{
            window.parent.postMessage({{
              type: 'tunnel-addon:install',
              addonId: 'audimo-streamers',
              url,
            }}, '*');
          }} else {{
            navigator.clipboard.writeText(url).then(() => {{
              urlBox.textContent = url + '\\n\\n(copied to clipboard — paste into Audimo → Addons → Install)';
            }});
          }}
        }});
      </script>
    </body>"""
