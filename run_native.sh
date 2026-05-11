#!/usr/bin/env bash
# Run the audimo_streamers addon natively on port 9006.
#
# This addon wraps yt-dlp + ytmusicapi + SoundCloud/Bandcamp scrapers
# to produce playable audio URLs from public web sources. No debrid,
# no torrents — pure HTTP fan-out.

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating venv (.venv/)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

ADDON_HOST="${AUDIMO_ADDON_HOST:-${TUNNEL_ADDON_HOST:-127.0.0.1}}"
echo "[run] starting audimo-streamers on http://${ADDON_HOST}:9006"

exec .venv/bin/uvicorn server:app \
  --host "${ADDON_HOST}" \
  --port 9006 \
  --proxy-headers \
  --no-access-log \
  --reload \
  --reload-dir "$(pwd)" \
  --reload-exclude ".venv/*"
