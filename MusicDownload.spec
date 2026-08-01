# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_all


PROJECT_DIR = os.path.abspath(SPECPATH)
MUSICDL_DATAS, MUSICDL_BINARIES, MUSICDL_HIDDEN_IMPORTS = collect_all("musicdl")

datas = MUSICDL_DATAS + [
    (os.path.join(PROJECT_DIR, "assets", "MusicDownload.png"), "assets"),
]

a = Analysis(
    [os.path.join(PROJECT_DIR, "musicdownload.py")],
    pathex=[PROJECT_DIR],
    binaries=MUSICDL_BINARIES,
    datas=datas,
    hiddenimports=MUSICDL_HIDDEN_IMPORTS + [
        "musicdownload_core",
        "PySide6.QtMultimedia",
        "av",
        "mutagen",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "nodejs_wheel", "nodejs_wheel_binaries"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicDownload",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MusicDownload",
)

app = BUNDLE(
    coll,
    name="MusicDownload.app",
    icon=os.path.join(PROJECT_DIR, "assets", "MusicDownload.icns"),
    bundle_identifier="com.musicdownload.desktop",
    info_plist={
        "CFBundleDisplayName": "MusicDownload",
        "CFBundleName": "MusicDownload",
        "CFBundleShortVersionString": "1.2.1",
        "CFBundleVersion": "121",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
