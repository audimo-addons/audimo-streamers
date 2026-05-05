# audimo-streamers

Audimo addon that plays YouTube, SoundCloud, and Bandcamp as native
Audimo sources. Free public web streams, no account required. Each
service can be toggled individually.

Distributed two ways:

1. **One-click install via Audimo's catalog** (recommended) — open
   Audimo → Addons → Catalog, click Install on "YouTube / SoundCloud
   / Bandcamp". The desktop app downloads the matching binary from
   the latest release here, verifies its SHA256, and runs it as a
   managed sidecar on a local port.

2. **Self-host as a paste-the-URL addon** — run the FastAPI server
   yourself (locally or on a public URL) and paste
   `http://your-host:9006` into Audimo's Addons tab.

## Capabilities

| Endpoint                     | Capability               | What it does                                                |
|------------------------------|--------------------------|-------------------------------------------------------------|
| `POST /resolve/sources`      | `resolve.sources`        | Fan-out search across YT/SC/Bandcamp, returns combined hits |
| `POST /resolve/sources/stream` | `resolve.sources.stream` | SSE variant — sections arrive as each service responds      |
| `POST /resolve/stream`       | `resolve.stream`         | Extracts a fresh audio URL for a chosen source              |
| `POST /cache/resolve`        | `cache.resolve`          | Re-extracts when a stored stream URL expires (~6h for YT)   |
| `POST /search/tracks`        | `search.tracks`          | Thin wrapper over `/resolve/sources` for clients            |

## Run locally (dev)

    bash run_native.sh
    # → addon listening on http://0.0.0.0:9006

Then in Audimo: Addons → Install → paste `http://localhost:9006`.

Toggles for each source live at `/configure` (browser-based settings
UI) or in the install URL's path-segment config blob.

## Build a binary (PyInstaller)

    bash build.sh
    # → dist/audimo-streamers (single-file binary)

The CI workflow at `.github/workflows/release.yml` runs the same
build on macOS arm64/x64, Linux x64, and Windows x64 runners and
publishes the four binaries plus a generated `manifest.json` to a
GitHub release. Tag `streamers-vX.Y.Z` to trigger.

## Hosted deploys

Set `AUDIMO_HOSTED=1` to mark the manifest as hosted (informational
flag). Set `AUDIMO_ADDON_KEY` to a long random string to require a
shared-secret header on every request — the Configure UI takes the
same key and bakes it into the install URL. Without `AUDIMO_ADDON_KEY`
the addon is wide-open and should not be exposed to the public
internet.

## Legal posture

This addon is a thin wrapper around publicly-accessible web sources
(yt-dlp + ytmusicapi for YouTube, SoundCloud's api-v2 with its own
client_id, Bandcamp's `data-tralbum` blob). Same legal posture as
yt-dlp itself: distributing the tool has substantial non-infringing
uses (archiving your own uploads, accessibility, journalism, fair-use
research). Operating a hosted instance for the public is a different
posture — see Audimo's docs for the tradeoff.
