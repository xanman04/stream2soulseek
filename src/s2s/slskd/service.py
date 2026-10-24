"""Run slskd as a child process the app owns, logged in with the user's stored Soulseek account.

The Soulseek username/password live only in the DPAPI SecretStore and reach slskd through the
SLSKD_SLSK_USERNAME / SLSKD_SLSK_PASSWORD environment variables of the child process; they are never
written to slskd.yml, logged, or put in exception messages. The generated slskd.yml holds only the
app's own random API key and web password, and lives in the per-user app dir (never the repository).
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import config
from ..models import Settings
from ..secretstore import SLSKD_API_KEY, SecretStore
from .client import SlskdClient, SlskdError

SOULSEEK_USERNAME = "soulseek_username"
SOULSEEK_PASSWORD = "soulseek_password"
SLSKD_WEB_PASSWORD = "slskd_web_password"

_PID_FILE = "s2s-slskd.pid"
_CREATE_NO_WINDOW = 0x08000000


class SlskdServiceError(Exception):
    pass


class SoulseekLoginError(SlskdServiceError):
    """slskd started but never logged in to the Soulseek server (bad credentials or no connectivity)."""


@dataclass(frozen=True)
class SoulseekCredentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never reveal the password
        return f"SoulseekCredentials(username={self.username!r}, password=***)"


# ------------------------------------------------------------------ stored login


def load_credentials(store: SecretStore) -> SoulseekCredentials | None:
    user, pw = store.get(SOULSEEK_USERNAME), store.get(SOULSEEK_PASSWORD)
    return SoulseekCredentials(user, pw) if user and pw else None


def save_credentials(store: SecretStore, creds: SoulseekCredentials) -> None:
    store.set(SOULSEEK_USERNAME, creds.username)
    store.set(SOULSEEK_PASSWORD, creds.password)


def clear_credentials(store: SecretStore) -> None:
    store.delete(SOULSEEK_USERNAME)
    store.delete(SOULSEEK_PASSWORD)


def validate_credentials(username: str, password: str) -> str | None:
    """Return an error message, or None if the input is plausible."""
    if not username.strip():
        return "Enter your Soulseek username."
    if username != username.strip():
        return "The username must not start or end with spaces."
    if len(username) > 30:
        return "Soulseek usernames are at most 30 characters."
    if not password:
        return "Enter your Soulseek password."
    return None


# ------------------------------------------------------------------ locating slskd


def find_slskd_exe(settings: Settings) -> Path | None:
    """settings.extra['slskd_exe'] if set, else the newest %LOCALAPPDATA%\\Programs\\slskd\\<version>\\slskd.exe."""
    explicit = settings.extra.get("slskd_exe")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "slskd"
    if not base.is_dir():
        return None

    def version_key(p: Path) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", p.parent.name)) or (0,)

    found = sorted(base.glob("*/slskd.exe"), key=version_key, reverse=True)
    return found[0] if found else None


def incomplete_dir_for(settings: Settings) -> str:
    """settings.extra['slskd_incomplete_dir'], else a 'downloading' folder beside the downloads folder."""
    explicit = settings.extra.get("slskd_incomplete_dir")
    if explicit:
        return str(explicit)
    return str(Path(settings.slskd_downloads_dir).parent / "downloading")


# ------------------------------------------------------------------ config


def _yaml_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _nc(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def _is_within(child: Path, parent: Path) -> bool:
    c, p = _nc(child), _nc(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def share_folder_problem(folder: str, *, incomplete_dir: str | None = None, app_dir: Path | None = None) -> str | None:
    """Why `folder` must not be shared, or None if it may be.

    Shared folders are visible to every Soulseek user, so refuse anything that could expose the app's secrets
    (its data folder or any folder containing it), whole drives, the user's home folder, and slskd's
    incomplete-downloads folder.
    """
    if not folder or not folder.strip():
        return "Empty folder path."
    p = Path(folder)
    if not p.is_absolute():
        return f"{folder}: use a full path."
    if not p.is_dir():
        return f"{folder}: folder not found."
    if p.resolve().parent == p.resolve():
        return f"{folder}: a whole drive cannot be shared."
    if _nc(p) == _nc(Path.home()):
        return f"{folder}: your whole user folder cannot be shared."
    app = app_dir or config.app_dir_path()
    if _is_within(app, p) or _is_within(p, app):
        return f"{folder}: it contains or is inside S2S's own data (saved logins), so it cannot be shared."
    if incomplete_dir and (_is_within(Path(incomplete_dir), p) or _is_within(p, Path(incomplete_dir))):
        return f"{folder}: the unfinished-downloads folder cannot be shared."
    return None


def render_config(*, downloads_dir: str, incomplete_dir: str, api_key: str, web_password: str, port: int,
                  shared_folders: list[str] = ()) -> str:
    if shared_folders:
        shares = "shares:\n  directories:\n" + "".join(f"    - {_yaml_str(f)}\n" for f in shared_folders) + (
            "  filters:                # never share these, even inside a shared folder\n"
            "    - \\.ini$\n    - Thumbs\\.db$\n    - \\.DS_Store$\n    - \\.s2s-partial$\n    - desktop\\.ini$\n")
    else:
        shares = "shares:\n  directories: []          # share nothing until the user chooses folders deliberately\n"
    return f"""# Generated by S2S on every start; edits here are overwritten.
# The Soulseek login is NOT stored here (it is passed to slskd through environment variables).
remote_configuration: false
remote_file_management: false

directories:
  incomplete: {_yaml_str(incomplete_dir)}
  downloads: {_yaml_str(downloads_dir)}

transfers:
  download:
    destination:
      subdirectory: '${{SOURCE_USERNAME}}/${{SOURCE_DIRECTORY}}'   # downloads\\<owner>\\<folder>\\<track>
      exists: rename

{shares}
retention:
  files:
    complete: ~            # never auto-delete downloaded files
    incomplete: ~

web:
  port: {port}
  ip_address: 127.0.0.1    # loopback only
  https:
    disabled: true
  authentication:
    disabled: false
    username: slskd
    password: {_yaml_str(web_password)}
    api_keys:
      s2s:
        key: {_yaml_str(api_key)}
        role: readwrite
        cidr: 127.0.0.1/32,::1/128

feature:
  swagger: false
"""


# ------------------------------------------------------------------ service


class SlskdService:
    """Owns one slskd child process. Not thread-safe; drive it from one worker at a time."""

    def __init__(
        self,
        settings: Settings | Callable[[], Settings],
        secret_store: SecretStore,
        *,
        app_dir: Path | None = None,
        exe: Path | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        http_get: Callable[..., httpx.Response] = httpx.get,
        client_factory: Callable[[str, str], SlskdClient] = SlskdClient,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # A callable is read on every use, so a downloads folder or shares saved in Settings take effect
        # without restarting the app (the Settings dialog replaces the Settings object rather than mutating it).
        self._get_settings: Callable[[], Settings] = settings if callable(settings) else (lambda: settings)
        self._secrets = secret_store
        self._dir = app_dir or (config.app_dir() / "slskd")
        self._exe = exe
        self._popen = popen
        self._http_get = http_get
        self._client_factory = client_factory
        self._sleep = sleep
        self._clock = clock
        self._proc: subprocess.Popen | None = None

    @property
    def _settings(self) -> Settings:
        return self._get_settings()

    @property
    def app_dir(self) -> Path:
        return self._dir

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _port(self) -> int:
        from urllib.parse import urlsplit

        return urlsplit(self._settings.slskd_url).port or 5030

    def _secret(self, name: str, nbytes: int) -> str:
        value = self._secrets.get(name)
        if not value:
            value = secrets.token_urlsafe(nbytes)
            self._secrets.set(name, value)
        return value

    def api_key(self) -> str:
        return self._secret(SLSKD_API_KEY, 48)

    def write_config(self) -> Path:
        if not self._settings.slskd_downloads_dir:
            raise SlskdServiceError("Set the slskd downloads folder in Settings first.")
        downloads = Path(self._settings.slskd_downloads_dir)
        incomplete = Path(incomplete_dir_for(self._settings))
        for d in (downloads, incomplete):
            d.mkdir(parents=True, exist_ok=True)
        shared = list(self._settings.shared_folders)
        for f in shared:  # re-checked on every start: never share something unsafe, even if settings were edited
            problem = share_folder_problem(f, incomplete_dir=str(incomplete), app_dir=self._dir.parent)
            if problem:
                raise SlskdServiceError(f"Sharing: {problem} Change it in Settings > General > Sharing.")
        self._dir.mkdir(parents=True, exist_ok=True)
        cfg = self._dir / "slskd.yml"
        tmp = cfg.with_suffix(".tmp")
        tmp.write_text(render_config(downloads_dir=str(downloads), incomplete_dir=str(incomplete),
                                     api_key=self.api_key(), web_password=self._secret(SLSKD_WEB_PASSWORD, 24),
                                     port=self._port(), shared_folders=shared), encoding="utf-8")
        os.replace(tmp, cfg)
        return cfg

    def _port_in_use(self) -> bool:
        try:
            self._http_get(f"{self._settings.slskd_url}/api/v0/application", timeout=2)
            return True
        except httpx.HTTPError:
            return False

    def _stop_previous(self) -> None:
        """Stop an slskd this app started in an earlier session (recorded in the pid file)."""
        pid_file = self._dir / _PID_FILE
        if not pid_file.exists():
            return
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid_file.unlink(missing_ok=True)
            return
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW)
        if "slskd" in out.stdout.lower():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=_CREATE_NO_WINDOW)
            deadline = self._clock() + 10
            while self._port_in_use() and self._clock() < deadline:
                self._sleep(0.5)
        pid_file.unlink(missing_ok=True)

    def start(self, creds: SoulseekCredentials, *, ready_timeout: float = 60, login_timeout: float = 30) -> None:
        """(Re)start slskd with `creds` and wait until it is logged in to Soulseek.

        Raises SoulseekLoginError if the API comes up but the login does not succeed in time.
        """
        exe = self._exe or find_slskd_exe(self._settings)
        if exe is None:
            raise SlskdServiceError("slskd is not installed (expected under %LOCALAPPDATA%\\Programs\\slskd).")
        self.stop()
        self._stop_previous()
        if self._port_in_use():
            raise SlskdServiceError(
                f"Another program is already using {self._settings.slskd_url}. Close it (for example an slskd "
                "you started yourself) and try again.")
        self.write_config()
        env = dict(os.environ)
        env["SLSKD_SLSK_USERNAME"] = creds.username
        env["SLSKD_SLSK_PASSWORD"] = creds.password
        log = open(self._dir / "slskd-console.log", "ab")  # slskd's own output; it does not print the password
        try:
            self._proc = self._popen([str(exe), "--app-dir", str(self._dir)], env=env, stdout=log,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=_CREATE_NO_WINDOW)
        finally:
            log.close()
        (self._dir / _PID_FILE).write_text(str(self._proc.pid))
        self._wait_logged_in(ready_timeout, login_timeout)

    def _wait_logged_in(self, ready_timeout: float, login_timeout: float) -> None:
        key = self.api_key()
        deadline = self._clock() + ready_timeout
        login_deadline: float | None = None
        while True:
            if self._proc is not None and self._proc.poll() is not None:
                raise SlskdServiceError("slskd exited during startup; see slskd-console.log in its app folder.")
            try:
                with self._client_factory(self._settings.slskd_url, key) as c:
                    if c.is_connected():
                        return
                if login_deadline is None:  # API is up; now give the Soulseek login its own time budget
                    login_deadline = self._clock() + login_timeout
            except SlskdError:
                pass
            now = self._clock()
            if login_deadline is not None and now > login_deadline:
                self.stop()
                raise SoulseekLoginError(
                    "Could not log in to Soulseek. Check the username and password, and that no other "
                    "Soulseek client (such as SoulseekQt) is using the same account.")
            if login_deadline is None and now > deadline:
                self.stop()
                raise SlskdServiceError("slskd did not start in time.")
            self._sleep(1.0)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc is not None:
            (self._dir / _PID_FILE).unlink(missing_ok=True)
