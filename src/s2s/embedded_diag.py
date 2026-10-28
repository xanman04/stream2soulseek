"""Navigation policy and a privacy-safe diagnostic trail for the embedded sign-in browsers.

Spotify's and SoundCloud's sign-in pages run inside S2S. When one fails ("Something went wrong") the cause is
invisible, so a few facts go to S2S's own log (Help > Report a problem includes it): the browser's warnings and
errors with query strings removed, any navigation the policy refused (scheme and frame only), and which of a short
list of known error phrases the page shows. Never page text, form values, cookies or tokens.
"""

from __future__ import annotations

import re

_QUERY = re.compile(r"[?#][^\s\"')]*")
# phrases whose presence on the page explains a failed sign-in; only the keys below are ever logged
_PHRASES = {
    "went-wrong": "something went wrong",
    "try-later": "try again later",
    "too-many": "too many",
    "incorrect": "incorrect",
    "verify-human": "verify you",
    "not-secure": "not secure",
    "unsupported": "browser is not supported",
    "blocked": "blocked",
}
# warnings every Chromium page produces for sites' own headers; they explain nothing about a failed sign-in
_NOISE = re.compile(r"Feature-Policy|Permissions-Policy|Unrecognized feature|requestStorageAccess|preload", re.I)
PROBE_JS = (
    "(function(){var t=((document.body&&document.body.innerText)||'').toLowerCase();var out=[];"
    + "".join(f"if(t.indexOf({text!r})>=0)out.push({key!r});" for key, text in _PHRASES.items())
    + "return out.join(',');})()"
)


def clean(text: str, limit: int = 240) -> str:
    """A log-safe copy of a browser message: queries and fragments removed, one line, shortened."""
    return _QUERY.sub("", " ".join(str(text).split()))[:limit]


def navigation_allowed(url, is_main_frame: bool) -> bool:
    """https everywhere; the page's own empty/inline frames and blobs only inside a page, never as the page."""
    scheme = url.scheme().lower()
    if scheme == "https" or url.toString() == "about:blank":
        return True
    return not is_main_frame and (url.toString() == "about:srcdoc" or scheme == "blob")


class Trail:
    """Writes the diagnostic trail for one embedded browser; `label` names it ("Spotify sign-in")."""

    def __init__(self, label: str) -> None:
        self.label = label

    def _log(self):
        from .applog import get

        return get()

    def console(self, level, message, line, source) -> None:
        name = getattr(level, "name", str(level))
        if "Info" in name or _NOISE.search(str(message)):
            return
        self._log().info("%s browser %s: %s (%s:%s)", self.label, name.replace("MessageLevel", "").lower(),
                         clean(message), clean(source, 80).rsplit("/", 1)[-1], line)

    def refused(self, url, is_main_frame: bool) -> None:
        self._log().info("%s: refused a %s navigation to scheme '%s'", self.label,
                         "page" if is_main_frame else "frame", url.scheme() or "?")

    def loaded(self, page, ok: bool) -> None:
        url = page.url()
        where = f"{url.host()}{url.path()}"
        self._log().info("%s: %s %s", self.label, "loaded" if ok else "FAILED to load", where)
        if not ok:
            return

        def seen(keys) -> None:
            if keys:
                self._log().info("%s: the page at %s shows: %s", self.label, where, keys)

        try:
            page.runJavaScript(PROBE_JS, seen)
        except Exception:  # noqa: BLE001 - diagnostics must never break sign-in
            pass
