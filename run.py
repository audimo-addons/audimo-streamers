"""PyInstaller / standalone launcher for the audimo-streamers addon.

`run_native.sh` covers dev mode. For frozen builds the binary needs an
actual `__main__` that boots uvicorn directly. Mirrors the same shape
audimo-aio and audimo-indexers use.
"""

import os

import uvicorn

from server import app


def main() -> None:
    host = os.getenv("AUDIMO_STREAMERS_HOST") or os.getenv("AUDIMO_ADDON_HOST", "0.0.0.0")
    port = int(os.getenv("AUDIMO_STREAMERS_PORT") or os.getenv("AUDIMO_ADDON_PORT", "9006"))
    # access_log=False mirrors the other addons: addon URLs may carry
    # a config blob in path segments and we don't want to log those.
    uvicorn.run(
        app, host=host, port=port,
        proxy_headers=True, log_level="info", access_log=False,
    )


if __name__ == "__main__":
    main()
