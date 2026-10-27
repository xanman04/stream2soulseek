"""Check GitHub Releases for a newer S2S, download its installer, verify its signature, and install it.

A release has only the installers people download: S2S-<ver>-windows-x64-setup.exe and S2S-<ver>-macos-<arch>.dmg.
Their Ed25519 signatures (made by the release build with the private key, see update_key.py) and SHA-256 digests
travel in a hidden comment in the release text (`signature_block()`), so the release page lists nothing else.

Flow: `check_for_update()` reads the latest release; `download_update()` fetches this platform's installer and
refuses it unless its signature matches; `stage_update()` prepares it (macOS: copies S2S.app out of the disk image
next to the app's real location, see `install_target()`); `launch_installer()` starts a small helper that waits for
S2S to quit, then runs the setup silently into the same folder (Windows) or swaps the app bundles (macOS), and
starts the new version. User data lives in the app data folder, so it is untouched.
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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
    notes: str  # release text without the hidden signature block
    archive_name: str  # the installer's file name
    archive_url: str
    signature: str  # base64 Ed25519 signature of (archive_name, sha256), from the release text
    page_url: str
    size_bytes: int | None = None


SIGNATURE_BLOCK = "s2s-update-signatures"
_BLOCK_RE = re.compile(r"<!--\s*" + SIGNATURE_BLOCK + r"\s*\n(.*?)-->", re.S)


def installer_name(version: str, target: str) -> str:
    return f"S2S-{version}-{target}-setup.exe" if target.startswith("windows") else f"S2S-{version}-{target}.dmg"


def signature_block(entries: dict[str, tuple[str, str]]) -> str:
    """The hidden comment appended to the release text: one line per installer, "<name> <sha256> <signature>"."""
    lines = "\n".join(f"{name} {digest} {sig}" for name, (digest, sig) in sorted(entries.items()))
    return f"\n\n<!-- {SIGNATURE_BLOCK}\n{lines}\n-->\n"


def read_signatures(body: str) -> dict[str, tuple[str, str]]:
    """{installer name: (sha256, signature)} from the release text; {} when there is no block."""
    match = _BLOCK_RE.search(body or "")
    out: dict[str, tuple[str, str]] = {}
    for line in (match.group(1) if match else "").splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[0]] = (parts[1].lower(), parts[2])
    return out


def visible_notes(body: str) -> str:
    return _BLOCK_RE.sub("", body or "").rstrip()


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
    body = str(release.get("body") or "")
    name = installer_name(version, target)
    signed = read_signatures(body).get(name)
    if not assets.get(name) or signed is None:
        return None  # no signed installer for this computer: not an update S2S may install
    size = next((a.get("size") for a in release.get("assets") or []
                 if isinstance(a, dict) and a.get("name") == name), None)
    size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None
    return UpdateInfo(version, visible_notes(body), name, assets[name], signed[1],
                      str(release.get("html_url") or RELEASES_PAGE), size)


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


def download_update(info: UpdateInfo, folder: Path, *, http_get=None, progress=None, status=None) -> Path:
    """Download and verify the installer. Returns its path; raises UpdateError when it is not genuine."""
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / info.archive_name
    partial = archive.with_name(archive.name + ".part")
    digest = hashlib.sha256()
    received = 0
    started = time.monotonic()

    def write(response):
        nonlocal received
        try:
            total = info.size_bytes or int(getattr(response, "headers", {}).get("content-length", 0))
        except (TypeError, ValueError):
            total = 0
        if progress:
            progress(0.0 if total else -1.0)
        chunks = response.iter_bytes(256 * 1024) if hasattr(response, "iter_bytes") else (response.content,)
        last_report = time.monotonic()
        with partial.open("wb") as stream:
            for chunk in chunks:
                stream.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                now = time.monotonic()
                if progress and total and (now - last_report >= 0.1 or received >= total):
                    progress(min(received / total, 1.0))
                    last_report = now
        if total and received != total:
            raise UpdateError("The update download was incomplete. Please try again.")

    try:
        if http_get is not None:
            write(http_get(info.archive_url))
        else:
            with _stream_get(info.archive_url) as response:
                write(response)
        if status:
            status("Verifying the downloaded update…")
        if not verify_signature(info.archive_name, digest.hexdigest(), info.signature):
            raise UpdateError("The downloaded update is not signed by the S2S developer, so it was not installed.")
        partial.replace(archive)
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        raise UpdateError("The update could not be downloaded. Try again later.") from exc
    if progress:
        progress(1.0)
    from .applog import get

    get().info("Update download: %d bytes verified in %.1f s", received, time.monotonic() - started)
    return archive


@contextmanager
def _stream_get(url: str):
    """Keep the response open while downloading; never buffer an entire installer in memory."""
    import httpx

    current = url
    with httpx.Client(timeout=60, follow_redirects=False, headers={"User-Agent": "S2S-updater"}) as client:
        for _ in range(5):
            parts = urlsplit(current)
            if parts.scheme != "https" or parts.hostname not in _DOWNLOAD_HOSTS:
                raise UpdateError("Refusing to download an update from outside GitHub.")
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    current = str(response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                yield response
                return
    raise UpdateError("Too many redirects while downloading the update.")


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
    """Prepare the verified installer. Windows: the setup program itself (run by the helper). macOS: S2S.app copied
    out of the disk image next to the install target (same disk, so the swap is a rename)."""
    if sys.platform != "darwin":
        return archive
    staging = Path(tempfile.mkdtemp(prefix=".s2s-update-", dir=app.parent))
    mount = Path(tempfile.mkdtemp(prefix="s2s-dmg-"))
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                        str(archive)], check=True, capture_output=True)
        try:  # ditto keeps the bundle's symlinks, permissions and signatures
            subprocess.run(["ditto", str(mount / app.name), str(staging / app.name)], check=True, capture_output=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update could not be unpacked.") from exc
    new_app = staging / app.name
    if not new_app.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update does not contain S2S.")
    return new_app


def installer_script(app: Path, new_app: Path, pid: int, *, appearance: dict | None = None) -> tuple[Path, list[str]]:
    """Write the helper that swaps the apps after S2S quits; returns (script path, command to start it).

    `app` is the install target; on macOS it may not exist yet (a translocated app installs into Applications).
    Windows: `new_app` is the verified setup program, run silently into `app` (the folder S2S runs from).
    """
    backup = app.with_name(f"{app.name}.old-{int(time.time())}")
    folder = Path(tempfile.mkdtemp(prefix="s2s-installer-"))
    if sys.platform == "win32":
        import json

        script = folder / "install.ps1"
        ps = lambda p: "'" + str(p).replace("'", "''") + "'"  # noqa: E731 - PowerShell single-quoted string
        values = {"APP": ps(app), "SETUP": ps(new_app), "PID": str(pid),
                  "RESULT": ps(result_file()), "ACTIVE": ps(active_file()),
                  "STYLE": ps(json.dumps(appearance or {}))}
        template = Path(__file__).with_name("update_windows.ps1").read_text(encoding="utf-8")
        script.write_text(re.sub(r"@@([A-Z]+)@@", lambda m: values[m[1]], template), encoding="utf-8-sig")
        icon = Path(__file__).parent / "ui" / "assets" / "s2s-icon.png"
        if icon.is_file():
            shutil.copyfile(icon, folder / "icon.png")
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(script)]
    else:
        script = folder / "install.sh"
        q = lambda p: "'" + str(p).replace("'", "'\\''") + "'"  # noqa: E731
        script.write_text(f"""#!/bin/sh
i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done
if [ -e {q(app)} ]; then mv {q(app)} {q(backup)} || {{ open {q(app)}; exit 1; }}; fi
if ! mv {q(new_app)} {q(app)}; then [ -e {q(backup)} ] && mv {q(backup)} {q(app)}; open {q(app)}; exit 1; fi
xattr -dr com.apple.quarantine {q(app)} 2>/dev/null
echo "installed 0" >> {q(result_file())}
open {q(app)}
rm -rf {q(backup)} {q(new_app.parent)} {q(folder)}
""", encoding="utf-8")
        script.chmod(0o755)
        cmd = ["/bin/sh", str(script)]
    return script, cmd


def result_file() -> Path:
    """Written before S2S quits to install ("pending <version>"); the helper adds "installed <code>" after."""
    from . import config

    return config.app_dir() / "update-result.txt"


def active_file() -> Path:
    from . import config

    return config.app_dir_path() / "update-active.json"


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: conservatively treat as running
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def active_update() -> dict | None:
    """Live helper record; stale/crashed helpers must not permanently block startup."""
    import json

    try:
        record = json.loads(active_file().read_text(encoding="utf-8-sig"))
        pid, started = record["pid"], record["started"]
        if (isinstance(pid, int) and pid > 0 and isinstance(started, (int, float))
                and 0 <= time.time() - started < 24 * 3600 and _pid_alive(pid)):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def prevent_launch_during_update() -> bool:
    """Before importing Qt: explain why a packaged Windows app cannot open yet."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    record = active_update()
    if not record or record.get("phase") != "installing":
        return False
    if os.path.normcase(str(Path(sys.executable).parent)) != os.path.normcase(str(record.get("target", ""))):
        return False
    import ctypes

    from ctypes import wintypes

    user = ctypes.windll.user32
    user.FindWindowW.restype = wintypes.HWND
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    window = user.FindWindowW(None, "Updating S2S")
    if window:
        user.ShowWindow(window, 9)  # restore the existing styled window, rather than another popup
        user.SetForegroundWindow(window)
    return True


def mark_pending(version: str) -> None:
    result_file().write_text(f"pending {version}\n", encoding="utf-8")


def failed_update(current: str) -> str | None:
    """At start: the version an update was meant to install when S2S is still older than it (the update did not
    install), else None. The record is removed either way."""
    path = result_file()
    try:
        text = path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    path.unlink(missing_ok=True)
    pending = text[1] if len(text) >= 2 and text[0] == "pending" else None
    return pending if pending and is_newer(pending, current) else None


def clean_leftovers(app: Path | None = None) -> list[Path]:
    """Remove what an update may have left behind (one that was interrupted, or one from S2S 1.0.x): downloaded
    installers, staging folders, the previous app kept as a backup, and helper scripts. Run at start, when no
    update can be in progress. Returns what was removed."""
    from . import config

    if active_update():
        return []  # never delete the installer/helper that is still running
    found = [config.app_dir() / "updates"]
    app = installed_app() if app is None else app
    if app is not None:
        found += [*app.parent.glob(".s2s-update-*"), *app.parent.glob(f"{app.name}.old-*")]
    tmp = Path(tempfile.gettempdir())
    found += [*tmp.glob("s2s-installer-*"), *tmp.glob("s2s-dmg-*")]
    removed = []
    for path in found:
        if not path.exists():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        if not path.exists():
            removed.append(path)
    return removed


def launch_installer(cmd: list[str]):
    """Start the helper so that it outlives S2S (S2S quits right after).

    Windows: a hidden console (CREATE_NO_WINDOW) in its own process group, broken away from any job S2S runs in.
    Not DETACHED_PROCESS: PowerShell started without a console exits at once without running the script (the
    reason no in-app update up to 1.1.2 installed on Windows).
    """
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB (literal values: _NO_WINDOW is 0 off Windows)
        no_window, new_group, breakaway = 0x08000000, 0x00000200, 0x01000000
        for flags in (no_window | new_group | breakaway, no_window | new_group):
            try:
                return subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError:  # a job that forbids breaking away: start inside it (it outlives S2S all the same)
                if not flags & breakaway:
                    raise
    else:
        return subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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
            if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
                raise UpdateError("GitHub is limiting update checks from this network for a while. Try again later.")
            response.raise_for_status()
            return response
    raise UpdateError("Too many redirects while downloading the update.")
