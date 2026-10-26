"""Check GitHub Releases for a newer S2S, download it, verify its signature, and swap it in.

Flow: `check_for_update()` reads the latest release; `download_update()` fetches this platform's archive and its
`.sig`, and refuses it unless the Ed25519 signature (made by the release build with the private key, see
update_key.py) matches; `stage_update()` unpacks it next to the app's real location (`install_target()`: on macOS
an app opened from where it was downloaded runs from a read-only "translocated" copy, so the original location, or
else the Applications folder, is used instead); `launch_installer()` starts a small
helper that waits for S2S to quit, swaps the folders (keeping the old one until the new one is in place) and
starts the new version. User data lives in the app data folder, so it is untouched.
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .update_key import PUBLIC_KEY_B64

REPO = "xanman04/stream2soulseek"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class UpdateError(Exception):
    """The update could not be checked, downloaded, verified or installed; the message is for the user."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    notes: str
    archive_name: str
    archive_url: str
    signature_url: str
    page_url: str


def platform_target() -> str | None:
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() in ("arm64", "aarch64") else "macos-x64"
    return None


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def check_for_update(current: str, *, http_get=None) -> UpdateInfo | None:
    """The latest release when it is newer than `current` and has a signed archive for this computer."""
    target = platform_target()
    if target is None:
        return None
    get = http_get or _http_get
    try:
        release = get(LATEST_API).json()
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("Could not check for updates. Check your internet connection.") from exc
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    version = str(release.get("tag_name") or "").lstrip("vV")
    if not version or not is_newer(version, current):
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or [] if isinstance(a, dict)}
    name = f"S2S-{version}-{target}.zip"
    if not assets.get(name) or not assets.get(name + ".sig"):
        return None  # no signed archive for this computer: not an update S2S may install
    return UpdateInfo(version, str(release.get("body") or ""), name, assets[name], assets[name + ".sig"],
                      str(release.get("html_url") or RELEASES_PAGE))


def signed_message(archive_name: str, sha256: str) -> bytes:
    """What the release build signs: the archive's name (version and platform) and its SHA-256."""
    return f"S2S update\n{archive_name}\n{sha256}\n".encode("utf-8")


def verify_signature(archive_name: str, sha256: str, signature_b64: str, public_key_b64: str = PUBLIC_KEY_B64) -> bool:
    from .ed25519 import verify  # plain Python: no native crypto library in the app (see ed25519.py)

    try:
        key = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except ValueError:
        return False
    return verify(key, signed_message(archive_name, sha256), signature)


def download_update(info: UpdateInfo, folder: Path, *, http_get=None, progress=None) -> Path:
    """Download and verify the archive. Returns its path; raises UpdateError when it is not genuine."""
    get = http_get or _http_get
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / info.archive_name
    try:
        signature = get(info.signature_url).text
        response = get(info.archive_url)
        archive.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("The update could not be downloaded. Try again later.") from exc
    if progress:
        progress(1.0)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not verify_signature(info.archive_name, digest, signature):
        archive.unlink(missing_ok=True)
        raise UpdateError("The downloaded update is not signed by the S2S developer, so it was not installed.")
    return archive


def installed_app() -> Path | None:
    """The folder (Windows) or S2S.app bundle (macOS) of the running packaged app; None when run from source."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        bundle = exe.parents[2]
        return bundle if bundle.suffix == ".app" else None
    return exe.parent


def _translocated(app: Path) -> bool:
    return "/AppTranslocation/" in app.as_posix()


def _original_path(app: Path) -> Path | None:
    """Where a translocated app really is (macOS Security framework), or None."""
    import ctypes

    try:
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                                               ctypes.c_bool]
        cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
        cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p,
                                                        ctypes.c_long]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        sec.SecTranslocateCreateOriginalPathForURL.restype = ctypes.c_void_p
        sec.SecTranslocateCreateOriginalPathForURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        raw = os.fsencode(str(app))
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
        if not url:
            return None
        try:
            original = sec.SecTranslocateCreateOriginalPathForURL(url, None)
        finally:
            cf.CFRelease(url)
        if not original:
            return None
        try:
            buf = ctypes.create_string_buffer(4096)
            if not cf.CFURLGetFileSystemRepresentation(original, True, buf, len(buf)):
                return None
        finally:
            cf.CFRelease(original)
        return Path(os.fsdecode(buf.value))
    except (OSError, AttributeError):
        return None


def _writable(folder: Path) -> bool:
    """A real write test (os.access ignores Windows ACLs and read-only macOS volumes)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".s2s-write-test-", dir=folder)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def install_target(app: Path | None = None) -> Path | None:
    """Where the updated app goes: normally where it runs. A translocated macOS app (opened from Downloads
    without being moved) runs from a read-only copy, so its original location is used, else Applications."""
    app = installed_app() if app is None else app
    if app is None:
        return None
    if sys.platform != "darwin" or not _translocated(app):
        return app
    candidates = [_original_path(app), Path("/Applications") / app.name, Path.home() / "Applications" / app.name]
    for target in candidates:
        if target is not None and not _translocated(target) and _writable(target.parent):
            return target
    return None


def can_install(target: Path | None) -> bool:
    return target is not None and _writable(target.parent)


def stage_update(archive: Path, app: Path) -> Path:
    """Unpack the verified archive next to the install target (same disk, so the swap is a rename)."""
    staging = Path(tempfile.mkdtemp(prefix=".s2s-update-", dir=app.parent))
    if sys.platform == "darwin":  # ditto keeps the bundle's symlinks, permissions and signatures
        subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)], check=True)
    else:
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise UpdateError("The update archive contains an unsafe path.")
            z.extractall(staging)
    new_app = staging / app.name
    if not new_app.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update archive does not contain S2S.")
    return new_app


def installer_script(app: Path, new_app: Path, pid: int) -> tuple[Path, list[str]]:
    """Write the helper that swaps the apps after S2S quits; returns (script path, command to start it).

    `app` is the install target; on macOS it may not exist yet (a translocated app installs into Applications).
    """
    backup = app.with_name(f"{app.name}.old-{int(time.time())}")
    folder = Path(tempfile.mkdtemp(prefix="s2s-installer-"))
    if sys.platform == "win32":
        script = folder / "install.ps1"
        ps = lambda p: "'" + str(p).replace("'", "''") + "'"  # noqa: E731 - PowerShell single-quoted string
        script.write_text(f"""$ErrorActionPreference = 'Stop'
$app = {ps(app)}; $new = {ps(new_app)}; $backup = {ps(backup)}
for ($i = 0; $i -lt 240 -and (Get-Process -Id {pid} -ErrorAction SilentlyContinue); $i++) {{ Start-Sleep -Milliseconds 500 }}
$moved = $false
for ($i = 0; $i -lt 60 -and -not $moved; $i++) {{
  try {{ Move-Item -LiteralPath $app -Destination $backup; $moved = $true }} catch {{ Start-Sleep -Milliseconds 500 }}
}}
if (-not $moved) {{ Start-Process -FilePath (Join-Path $app 'S2S.exe'); exit 1 }}
try {{ Move-Item -LiteralPath $new -Destination $app }} catch {{ Move-Item -LiteralPath $backup -Destination $app; Start-Process -FilePath (Join-Path $app 'S2S.exe'); exit 1 }}
Start-Process -FilePath (Join-Path $app 'S2S.exe')
Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Split-Path $new) -Recurse -Force -ErrorAction SilentlyContinue
""", encoding="utf-8")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)]
    else:
        script = folder / "install.sh"
        q = lambda p: "'" + str(p).replace("'", "'\\''") + "'"  # noqa: E731
        script.write_text(f"""#!/bin/sh
i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done
if [ -e {q(app)} ]; then mv {q(app)} {q(backup)} || {{ open {q(app)}; exit 1; }}; fi
if ! mv {q(new_app)} {q(app)}; then [ -e {q(backup)} ] && mv {q(backup)} {q(app)}; open {q(app)}; exit 1; fi
xattr -dr com.apple.quarantine {q(app)} 2>/dev/null
open {q(app)}
rm -rf {q(backup)} {q(new_app.parent)}
""", encoding="utf-8")
        script.chmod(0o755)
        cmd = ["/bin/sh", str(script)]
    return script, cmd


def launch_installer(cmd: list[str]) -> None:
    """Start the helper detached from S2S (S2S quits right after)."""
    if sys.platform == "win32":
        subprocess.Popen(cmd, creationflags=_NO_WINDOW | 0x00000008,  # DETACHED_PROCESS
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def _http_get(url: str):
    """HTTPS GET limited to GitHub's release hosts (redirects followed only within them)."""
    import httpx

    current = url
    with httpx.Client(timeout=60, follow_redirects=False, headers={"User-Agent": "S2S-updater",
                                                                    "Accept": "application/vnd.github+json"
                                                                    if "api.github.com" in url else "*/*"}) as client:
        for _ in range(5):
            parts = urlsplit(current)
            if parts.scheme != "https" or (parts.hostname not in _DOWNLOAD_HOSTS and parts.hostname != "api.github.com"):
                raise UpdateError("Refusing to download an update from outside GitHub.")
            response = client.get(current)
            if response.is_redirect:
                current = str(response.headers.get("location", ""))
                continue
            response.raise_for_status()
            return response
    raise UpdateError("Too many redirects while downloading the update.")
