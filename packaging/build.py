"""Build the standalone S2S app for this computer: Windows (x64) or macOS (Apple silicon or Intel).

    python packaging/build.py

Steps: download the pinned slskd and FFmpeg builds (each checked against its published SHA-256), run PyInstaller,
copy the tools and licence notices into the app, sign the macOS app ad hoc, run the app's self-test, and write
the release archive to dist/. Runs from a clean checkout; nothing from this computer's S2S data is included.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "package"
DIST = ROOT / "dist"
CACHE = ROOT / "build" / "downloads"

SLSKD = {
    "windows-x64": ("https://github.com/slskd/slskd/releases/download/0.26.2/slskd-0.26.2-win-x64.zip",
                    "ffe186ecbedce15bf19b6a690c6213c669f34acc793ef53aa92390a5cd9c48f0"),
    "macos-arm64": ("https://github.com/slskd/slskd/releases/download/0.26.2/slskd-0.26.2-osx-arm64.zip",
                    "18d3ebf7760cd93296b61f640c87b383b41a142cd445a04a633d3b596313ef85"),
    "macos-x64": ("https://github.com/slskd/slskd/releases/download/0.26.2/slskd-0.26.2-osx-x64.zip",
                  "1df5c0cbe3b44dd6fae78f2461b5dd3353930a3c5b4128a8309d63b6bd435d95"),
}
_RIEDL = "https://ffmpeg.martin-riedl.de/download/macos"
FFMPEG = {
    "windows-x64": [("https://github.com/GyanD/codexffmpeg/releases/download/2026-10-22-git-bf0e831606/"
                     "ffmpeg-2026-10-22-git-bf0e831606-essentials_build.zip",
                     "07de5469e123c02e06959cbf7cc9e405c29f0530abde67f0721fe57dfe5e1fb4")],
    "macos-arm64": [(f"{_RIEDL}/arm64/1789931890_9.0.2/ffmpeg.zip",
                     "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
                    (f"{_RIEDL}/arm64/1789931890_9.0.2/ffprobe.zip",
                     "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6")],
    "macos-x64": [(f"{_RIEDL}/amd64/1789931006_9.0.2/ffmpeg.zip",
                   "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
                  (f"{_RIEDL}/amd64/1789931006_9.0.2/ffprobe.zip",
                   "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd")],
}
TEXTS = {  # licence texts referenced by THIRD_PARTY_NOTICES.md
    "LGPL-3.0.txt": "https://www.gnu.org/licenses/lgpl-3.0.txt",
    "slskd/LICENSE": "https://raw.githubusercontent.com/slskd/slskd/0.26.2/LICENSE",
}
NOT_BUNDLED = {"pip", "setuptools", "wheel", "pytest", "pluggy", "iniconfig", "pyinstaller", "pyinstaller-hooks-contrib",
               "altgraph", "pefile", "pywin32-ctypes", "macholib", "pillow", "s2s", "packaging"}


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def version() -> str:
    text = (ROOT / "src" / "s2s" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def target() -> str:
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() in ("arm64", "aarch64") else "macos-x64"
    raise SystemExit("S2S packages are built on Windows or macOS")


def fetch(url: str, sha256: str | None = None) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:10] + "-" + url.rsplit("/", 1)[-1]
    path = CACHE / name
    if not path.exists():
        log(f"downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "s2s-build"})
        with urllib.request.urlopen(req, timeout=300) as r, open(path.with_suffix(".part"), "wb") as f:
            shutil.copyfileobj(r, f)
        path.with_suffix(".part").replace(path)
    if sha256:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sha256:
            path.unlink()
            raise SystemExit(f"checksum mismatch for {url}: {digest}")
    return path


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def unzip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            out = (dest / info.filename).resolve()
            if not str(out).startswith(str(dest.resolve())):
                raise SystemExit(f"unsafe path in {archive.name}: {info.filename}")
            z.extract(info, dest)
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                out.chmod(mode & 0o777 or 0o644)


def prepare_tools(kind: str) -> Path:
    tools = WORK / "tools"
    shutil.rmtree(tools, ignore_errors=True)
    (tools / "slskd").mkdir(parents=True)
    unzip(fetch(*SLSKD[kind]), tools / "slskd")
    exe = "slskd.exe" if kind.startswith("windows") else "slskd"
    if not (tools / "slskd" / exe).is_file():
        raise SystemExit(f"{exe} not found in the slskd archive")
    staging = WORK / "ffmpeg"
    shutil.rmtree(staging, ignore_errors=True)
    for url, sha in FFMPEG[kind]:
        unzip(fetch(url, sha), staging)
    for name in ("ffmpeg", "ffprobe"):
        filename = name + (".exe" if kind.startswith("windows") else "")
        found = next(staging.rglob(filename), None)
        if found is None:
            raise SystemExit(f"{filename} not found in the FFmpeg archives")
        shutil.copy2(found, tools / filename)
    if not kind.startswith("windows"):
        for p in [tools / "ffmpeg", tools / "ffprobe", tools / "slskd" / "slskd"]:
            make_executable(p)
    return tools


def prepare_licenses() -> Path:
    lic = WORK / "licenses"
    shutil.rmtree(lic, ignore_errors=True)
    lic.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", lic / "LICENSE")
    shutil.copy2(ROOT / "LICENSE", lic / "GPL-3.0.txt")
    shutil.copy2(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md", lic / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "src" / "s2s" / "ui" / "assets" / "logos" / "NOTICE.md", lic / "logos-NOTICE.md")
    for rel, url in TEXTS.items():
        (lic / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fetch(url), lic / rel)
    python_license = next((p for p in (Path(sys.base_prefix) / "LICENSE.txt",
                                       Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}."
                                       f"{sys.version_info.minor}" / "LICENSE.txt") if p.is_file()), None)
    (lic / "python").mkdir()
    if python_license is None:
        python_license = fetch(f"https://raw.githubusercontent.com/python/cpython/v{platform.python_version()}/LICENSE")
    shutil.copy2(python_license, lic / "python" / "LICENSE")
    for dist in md.distributions():
        name = (dist.metadata["Name"] or "").lower()
        if not name or name in NOT_BUNDLED:
            continue
        files = [f for f in (dist.files or []) if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", f.name, re.I)]
        if not files:
            continue
        out = lic / "python-packages" / f"{name}-{dist.version}"
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = Path(dist.locate_file(f))
            if src.is_file():
                shutil.copy2(src, out / f.name)
    return lic


def run_pyinstaller(ver: str) -> None:
    env = dict(os.environ, S2S_VERSION=ver)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(WORK / "pyinstaller"),
                    str(ROOT / "packaging" / "s2s.spec")], check=True, env=env, cwd=ROOT)


def codesign(path: Path, *, deep: bool = False) -> None:
    args = ["codesign", "--force", "--sign", "-", "--timestamp=none"] + (["--deep"] if deep else []) + [str(path)]
    subprocess.run(args, check=True)


def assemble(kind: str, tools: Path, lic: Path) -> tuple[Path, Path]:
    """Copy tools and licences into the app; return (app folder or bundle, executable)."""
    if kind.startswith("windows"):
        app = DIST / "S2S"
        shutil.copytree(tools, app / "tools", dirs_exist_ok=True)
        shutil.copytree(lic, app / "licenses", dirs_exist_ok=True)
        return app, app / "S2S.exe"
    app = DIST / "S2S.app"
    resources = app / "Contents" / "Resources"
    shutil.copytree(tools, resources / "tools", dirs_exist_ok=True, symlinks=True)
    shutil.copytree(lic, resources / "licenses", dirs_exist_ok=True)
    for p in [resources / "tools" / "ffmpeg", resources / "tools" / "ffprobe", resources / "tools" / "slskd" / "slskd"]:
        make_executable(p)
        codesign(p)  # Apple silicon runs only signed code; ad hoc is enough without a Developer ID
    codesign(app, deep=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    return app, app / "Contents" / "MacOS" / "S2S"


def self_test(exe: Path) -> None:
    report = WORK / "self-test.txt"
    report.unlink(missing_ok=True)
    log(f"self-test: {exe}")
    proc = subprocess.run([str(exe), "--self-test", str(report)], timeout=300)
    text = report.read_text(encoding="utf-8") if report.exists() else "(no report)"
    print(text, flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"the packaged app's self-test failed (exit code {proc.returncode})")


def archive(kind: str, app: Path, ver: str) -> Path:
    out = DIST / f"S2S-{ver}-{kind}.zip"
    out.unlink(missing_ok=True)
    if kind.startswith("windows"):
        shutil.make_archive(str(out.with_suffix("")), "zip", root_dir=DIST, base_dir=app.name)
    else:  # ditto keeps the bundle's symlinks, permissions and signatures intact
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(out)], check=True)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / (out.name + ".sha256")).write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    log(f"wrote {out} ({out.stat().st_size // (1024 * 1024)} MiB, sha256 {digest})")
    return out


def main() -> None:
    kind, ver = target(), version()
    log(f"building S2S {ver} for {kind}")
    shutil.rmtree(DIST, ignore_errors=True)
    tools = prepare_tools(kind)
    lic = prepare_licenses()
    run_pyinstaller(ver)
    app, exe = assemble(kind, tools, lic)
    self_test(exe)
    archive(kind, app, ver)


if __name__ == "__main__":
    main()
