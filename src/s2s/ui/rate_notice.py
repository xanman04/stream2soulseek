"""A short notice when a search starts while other searches have already used much of Soulseek's shared allowance.

S2S may start only so many Soulseek searches in a window (`slskd.search.RATE_LIMIT` per `RATE_WINDOW_S`), however many
transfers are open, so the limit that keeps the account from being banned can never be exceeded. When another search
has just used a good part of that allowance, a new one is slower than usual; this says so, once per start.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QMessageBox, QWidget

from .. import config
from ..slskd import search as search_mod

HIDE_KEY = "rate_limit_notice_hidden"


def slowdown_text(expected: int, available: int) -> str | None:
    """The notice's explanation, or None when there is nothing to warn about: little of the allowance is used, or
    what is left covers the searches this one expects."""
    limit, window = search_mod.RATE_LIMIT, search_mod.RATE_WINDOW_S
    used = limit - available
    if used < max(1, limit // 3) or expected <= available:
        return None
    minutes = max(1, math.ceil((expected - available) * window / limit / 60))
    every = max(1, round(window / 60))
    return (f"Soulseek only lets S2S start {limit} searches every {every} minutes, and that allowance is shared by "
            f"everything S2S searches for. Other searches have just used {used} of them, so this one runs slower for "
            f"a while: at least {minutes} {'minute' if minutes == 1 else 'minutes'} more than usual.\n\n"
            "Nothing is wrong and your downloads are not affected. The limit protects your Soulseek account from a "
            "ban, so S2S never goes faster than this.")


def maybe_show(parent: QWidget | None, settings, expected: int, *, available: int | None = None) -> QMessageBox | None:
    """Open the notice (without blocking the search) when it applies and has not been switched off."""
    extra = getattr(settings, "extra", None)
    if isinstance(extra, dict) and extra.get(HIDE_KEY):
        return None
    text = slowdown_text(expected, search_mod.LIMITER.available() if available is None else available)
    if text is None:
        return None
    box = QMessageBox(QMessageBox.Icon.Information, "Searches will be slower", "Searches will be slower for a while",
                      QMessageBox.StandardButton.Ok, parent)
    box.setInformativeText(text)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    check = QCheckBox("Don't show this again")
    box.setCheckBox(check)

    def finished(_result: int) -> None:
        if check.isChecked() and isinstance(extra, dict):
            extra[HIDE_KEY] = True
            try:
                config.save_settings(settings)
            except Exception:  # noqa: BLE001 - a notice that cannot be remembered may simply show again
                pass

    box.finished.connect(finished)
    box.open()
    return box
