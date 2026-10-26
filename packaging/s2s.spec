# -*- mode: python -*-
# PyInstaller spec for the standalone S2S app. Run through packaging/build.py, which also adds the bundled tools
# (slskd, ffmpeg, ffprobe) and the licence notices, signs the macOS app and makes the release archive.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
VERSION = os.environ.get("S2S_VERSION", "0.0.0")
ICON_PNG = str(ROOT / "src" / "s2s" / "ui" / "assets" / "s2s-icon.png")

datas, binaries, hidden = [], [], []
for package in ("spotapi", "curl_cffi"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h
datas += collect_data_files("s2s", includes=["ui/assets/*.png", "ui/assets/logos/*"])
if sys.platform == "darwin":
    hidden += ["keyring.backends.macOS"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "s2s_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["pytest", "tkinter", "_pytest"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="S2S",
    console=False,  # a windowed app: no console window, ever
    icon=ICON_PNG,
)
coll = COLLECT(exe, a.binaries, a.datas, name="S2S")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="S2S.app",
        icon=ICON_PNG,
        bundle_identifier="io.github.xanman04.s2s",
        version=VERSION,
        info_plist={
            "CFBundleName": "S2S",
            "CFBundleDisplayName": "S2S",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.4",  # minimum supported by the pinned Qt 6.12 build
            "NSHumanReadableCopyright": "Copyright (C) 2026 xanman04. GNU GPL v3 or later.",
        },
    )
