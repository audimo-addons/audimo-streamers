#!/usr/bin/env bash
# Build audimo-streamers as a single-file PyInstaller binary.
#
# Output: dist/audimo-streamers (or audimo-streamers.exe on Windows).
#
# CI usage is identical — the GitHub Actions workflow at
# .github/workflows/release.yml runs this on macOS arm64/x64,
# Ubuntu, and Windows runners and uploads each artifact to a release.

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "[build] creating .venv"
  python3 -m venv .venv
fi

.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip install --quiet pyinstaller

rm -rf build dist

.venv/bin/pyinstaller audimo_streamers.spec --clean --noconfirm

echo "[build] dist/audimo-streamers ready"
