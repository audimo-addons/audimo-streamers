# PyInstaller spec for the audimo-streamers addon.
#
# Build with:
#   cd addons/audimo_streamers && source .venv/bin/activate && pyinstaller audimo_streamers.spec --clean
#
# This addon wraps yt-dlp + ytmusicapi + SC/Bandcamp scrapers. yt-dlp
# pulls in lots of optional extractor modules; we explicitly include
# the package so PyInstaller picks them up. ytmusicapi ships its
# locale resources via package data — `collect_all` keeps those
# alongside the binary.
#
# Output: dist/audimo-streamers (single-file binary).

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# yt-dlp ships ~1700 site-specific extractor modules. Without
# collect_submodules they get tree-shaken and the binary fails on
# every site-specific URL. Adds ~30MB but it's the difference
# between "works" and "doesn't".
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hidden = collect_all('yt_dlp')
ytm_datas, ytm_binaries, ytm_hidden = collect_all('ytmusicapi')


a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=yt_dlp_binaries + ytm_binaries,
    datas=yt_dlp_datas + ytm_datas,
    hiddenimports=(
        [
            'server',
            'extractors',
            'extractors.youtube',
            'extractors.soundcloud',
            'extractors.bandcamp',
            'uvicorn.lifespan.on',
            'uvicorn.lifespan.off',
            'uvicorn.loops.auto',
            'uvicorn.loops.asyncio',
            'uvicorn.loops.uvloop',
            'uvicorn.protocols.http.auto',
            'uvicorn.protocols.http.h11_impl',
            'uvicorn.protocols.http.httptools_impl',
            'uvicorn.protocols.websockets.auto',
            'uvicorn.protocols.websockets.websockets_impl',
            'uvicorn.protocols.websockets.wsproto_impl',
        ]
        + yt_dlp_hidden
        + ytm_hidden
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='audimo-streamers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
