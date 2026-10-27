"""The "Matching rules" editor: which files the user accepts. Embedded in the Settings dialog.

Defaults are the owner's rules. A different remix or a VIP mismatch is never accepted and has no switch.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from .. import layouts as lay_mod
from ..matching.formats import NAMES
from ..models import DEFAULT_MIN_KBPS, FORMAT_KINDS, LOSSY_KINDS, FormatRule, MatchPolicy
from . import theme

EXCLUSION_HELP = (
    "S2S always looks for the version in your playlist: a radio edit or live recording is never used for the "
    "normal track, and a radio edit or live track in your playlist is looked for as that version."
)
CLEAN_HELP = "Clean versions are still used when the track in your playlist is itself the clean version."
MIXED_HELP = ("For a track taken from a DJ mix (for example \"Song - Mixed\", often only a short excerpt), S2S "
              "looks for the song itself; mixed files are never used. Turn this off to look for the mix instead.")
NEVER_NOTE = "A different remix or remixer, a different version, and a VIP mismatch are never accepted."


CUSTOM = "(unsaved rules)"


def _ask_name(parent: QWidget, current: str) -> str | None:
    name, ok = QInputDialog.getText(parent, "Save layout", "Layout name:", text=current)
    return name if ok else None


def _confirm(parent: QWidget, text: str) -> bool:
    return QMessageBox.question(parent, "Layouts", text) == QMessageBox.StandardButton.Yes


class MatchingRulesWidget(QWidget):
    def __init__(self, policy: MatchPolicy, parent: QWidget | None = None,
                 layouts: dict[str, MatchPolicy] | None = None,
                 ask_name: Callable[[QWidget, str], str | None] = _ask_name,
                 confirm: Callable[[QWidget, str], bool] = _confirm) -> None:
        super().__init__(parent)
        self._lossy: dict[str, tuple[int, bool]] = {}
        self._current_kind: str | None = None
        self.layouts: dict[str, MatchPolicy] = dict(layouts or {})  # edited copy; the dialog saves it
        self._ask_name = ask_name
        self._confirm = confirm

        # ---- saved layouts
        self.layout_combo = QComboBox()
        self.load_btn = QPushButton("Load")
        self.save_as_btn = QPushButton("Save as...")
        self.delete_btn = QPushButton("Delete")
        lrow = QHBoxLayout()
        lrow.addWidget(self.layout_combo, 1)
        for b in (self.load_btn, self.save_as_btn, self.delete_btn):
            lrow.addWidget(b)
        lbox = QGroupBox("Layouts (saved sets of these rules; kept when you press Save)")
        QVBoxLayout(lbox).addLayout(lrow)

        # ---- formats
        self.formats = QListWidget()
        self.formats.setMaximumHeight(170)
        self.up_btn = QPushButton("Move up")
        self.down_btn = QPushButton("Move down")
        self.min_kbps = QSpinBox()
        self.min_kbps.setRange(0, 320)
        self.min_kbps.setSuffix(" kbps")
        self.allow_vbr = QCheckBox("Also accept variable bitrate (VBR)")
        self.fmt_detail = QLabel("")
        side = QVBoxLayout()
        side.addWidget(self.up_btn)
        side.addWidget(self.down_btn)
        side.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.formats, 1)
        top.addLayout(side)
        detail = QFormLayout()
        detail.addRow(self.fmt_detail)
        detail.addRow("Minimum bitrate:", self.min_kbps)
        detail.addRow("", self.allow_vbr)
        fbox = QGroupBox("Accepted file types (tick to accept; the order is the priority, best first)")
        fl = QVBoxLayout(fbox)
        fl.addLayout(top)
        fl.addLayout(detail)

        # ---- versions
        self.mode_exact = QRadioButton("Exact version only")
        self.mode_ext = QRadioButton("Exact version, or extended mix of the same version if no exact match")
        self.tier_first = QCheckBox("Prefer the right version over a better format")
        vbox = QGroupBox("Versions")
        vl = QVBoxLayout(vbox)
        vl.addWidget(self.mode_exact)
        vl.addWidget(self.mode_ext)
        vl.addWidget(self.tier_first)
        vl.addWidget(QLabel(NEVER_NOTE))

        # ---- labelled versions: only the choices that change anything
        self.ex_clean = QCheckBox("Reject clean (censored) versions")
        self.ex_mixed = QCheckBox("For DJ-mixed tracks in your playlist, find the song itself")
        ebox = QGroupBox("Clean and DJ-mixed versions")
        el = QVBoxLayout(ebox)
        for w, text in ((self.ex_clean, CLEAN_HELP), (self.ex_mixed, MIXED_HELP), (None, EXCLUSION_HELP)):
            if w is not None:
                el.addWidget(w)
            note = theme.subtle(QLabel(text))
            note.setWordWrap(True)
            note.setContentsMargins(22 if w is not None else 0, 0, 0, 6)
            el.addWidget(note)

        # ---- review behaviour
        self.tolerance = QSpinBox()
        self.tolerance.setRange(0, 600)
        self.tolerance.setSuffix(" s")
        self.skip_flagged = QCheckBox("Leave flagged matches unticked in Review")
        rbox = QGroupBox("Review")
        rl = QFormLayout(rbox)
        rl.addRow("Flag a match when its length differs by more than:", self.tolerance)
        rl.addRow("", self.skip_flagged)

        self.restore_btn = QPushButton("Restore defaults")
        foot = QHBoxLayout()
        foot.addWidget(self.restore_btn)
        foot.addStretch(1)

        lay = QVBoxLayout(self)
        for w in (lbox, fbox, vbox, ebox, rbox):
            lay.addWidget(w)
        lay.addLayout(foot)
        lay.addStretch(1)

        self.formats.currentRowChanged.connect(self._row_changed)
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.restore_btn.clicked.connect(self.restore_defaults)
        self.load_btn.clicked.connect(self.load_selected)
        self.save_as_btn.clicked.connect(self.save_as)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.layout_combo.currentIndexChanged.connect(lambda _i: self._update_layout_buttons())
        self.set_policy(policy)
        self.refresh_layouts()

    # ------------------------------------------------------------ layouts

    def refresh_layouts(self, select: str | None = None) -> None:
        """Fill the combo; select `select`, else the layout equal to the shown rules, else '(unsaved rules)'."""
        if select is None:
            try:
                fp = self.policy().fingerprint()
            except ValueError:
                fp = None
            select = next((n for n, p in lay_mod.all_layouts(self.layouts) if p.fingerprint() == fp), CUSTOM)
        self.layout_combo.blockSignals(True)
        self.layout_combo.clear()
        if select == CUSTOM:
            self.layout_combo.addItem(CUSTOM, None)
        for name, _p in lay_mod.all_layouts(self.layouts):
            self.layout_combo.addItem(name, name)
        i = self.layout_combo.findData(select if select != CUSTOM else None)
        self.layout_combo.setCurrentIndex(max(i, 0))
        self.layout_combo.blockSignals(False)
        self._update_layout_buttons()

    def selected_layout(self) -> str | None:
        return self.layout_combo.currentData()

    def _update_layout_buttons(self) -> None:
        name = self.selected_layout()
        self.load_btn.setEnabled(name is not None)
        self.delete_btn.setEnabled(name is not None and name != lay_mod.DEFAULT_NAME)

    def load_selected(self) -> None:
        name = self.selected_layout()
        if name is None:
            return
        self.set_policy(lay_mod.layout_policy(self.layouts, name))
        self.refresh_layouts(select=name)

    def save_as(self) -> None:
        try:
            policy = self.policy()
        except ValueError as exc:
            QMessageBox.warning(self, "Layouts", str(exc))
            return
        current = self.selected_layout()
        suggestion = current if current and current != lay_mod.DEFAULT_NAME else ""
        name = self._ask_name(self, suggestion)
        if name is None:
            return
        try:
            clean = lay_mod.validate_name(name)
        except lay_mod.LayoutError as exc:
            QMessageBox.warning(self, "Layouts", str(exc))
            return
        if lay_mod.find(self.layouts, clean) is not None and not self._confirm(
                self, f'Replace the layout "{clean}" with the rules shown?'):
            return
        stored = lay_mod.save_layout(self.layouts, clean, policy)
        self.refresh_layouts(select=stored)

    def delete_selected(self) -> None:
        name = self.selected_layout()
        if name is None or name == lay_mod.DEFAULT_NAME:
            return
        if not self._confirm(self, f'Delete the layout "{name}"? The rules shown stay as they are.'):
            return
        lay_mod.delete_layout(self.layouts, name)
        self.refresh_layouts()

    # ------------------------------------------------------------ formats list

    def _kind_at(self, row: int) -> str:
        return self.formats.item(row).data(Qt.ItemDataRole.UserRole)

    def _store_detail(self) -> None:
        k = self._current_kind
        if k in LOSSY_KINDS:
            self._lossy[k] = (self.min_kbps.value(), self.allow_vbr.isChecked())

    def _row_changed(self, row: int) -> None:
        self._store_detail()
        self._current_kind = self._kind_at(row) if row >= 0 else None
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        k = self._current_kind
        lossy = k in LOSSY_KINDS
        self.min_kbps.setEnabled(lossy)
        self.allow_vbr.setEnabled(lossy)
        if lossy:
            lo, vbr = self._lossy.get(k, (DEFAULT_MIN_KBPS[k], False))
            self.min_kbps.setValue(lo)
            self.allow_vbr.setChecked(vbr)
            self.fmt_detail.setText(f"{NAMES[k]} is lossy: set the lowest bitrate you accept.")
        else:
            self.fmt_detail.setText("" if k is None else f"{NAMES[k]} is lossless: no bitrate setting.")

    def _move(self, delta: int) -> None:
        row = self.formats.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < self.formats.count():
            return
        self._store_detail()
        self.formats.blockSignals(True)
        it = self.formats.takeItem(row)
        self.formats.insertItem(new, it)
        self.formats.setCurrentRow(new)
        self.formats.blockSignals(False)
        self._current_kind = self._kind_at(new)

    # ------------------------------------------------------------ policy <-> widgets

    def set_policy(self, p: MatchPolicy) -> None:
        self.formats.blockSignals(True)
        self.formats.clear()
        chosen = {r.kind: r for r in p.formats}
        self._lossy = {k: (r.min_bitrate_kbps or 0, r.allow_vbr) for k, r in chosen.items() if r.lossy}
        order = [r.kind for r in p.formats] + [k for k in FORMAT_KINDS if k not in chosen]
        for kind in dict.fromkeys(order):
            it = QListWidgetItem(NAMES[kind])
            it.setData(Qt.ItemDataRole.UserRole, kind)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if kind in chosen else Qt.CheckState.Unchecked)
            self.formats.addItem(it)
        self.formats.setCurrentRow(0)
        self.formats.blockSignals(False)
        self._current_kind = self._kind_at(0)
        self._refresh_detail()
        (self.mode_exact if p.version_mode == "exact_only" else self.mode_ext).setChecked(True)
        self.tier_first.setChecked(p.tier_before_format)
        self.ex_mixed.setChecked(p.exclude_mixed)
        self.ex_clean.setChecked(p.exclude_clean)
        self.tolerance.setValue(p.duration_tolerance_s)
        self.skip_flagged.setChecked(p.skip_flagged_by_default)

    def restore_defaults(self) -> None:
        self.set_policy(MatchPolicy())
        self.refresh_layouts()

    def policy(self) -> MatchPolicy:
        """The policy shown. Raises ValueError when no file type is ticked."""
        self._store_detail()
        rules: list[FormatRule] = []
        for i in range(self.formats.count()):
            it = self.formats.item(i)
            if it.checkState() != Qt.CheckState.Checked:
                continue
            kind = it.data(Qt.ItemDataRole.UserRole)
            if kind in LOSSY_KINDS:
                lo, vbr = self._lossy.get(kind, (DEFAULT_MIN_KBPS[kind], False))
                rules.append(FormatRule(kind, lo, vbr))
            else:
                rules.append(FormatRule(kind))
        if not rules:
            raise ValueError("Tick at least one accepted file type under Matching rules.")
        return MatchPolicy(
            formats=rules,
            version_mode="exact_only" if self.mode_exact.isChecked() else "exact_then_extended",
            tier_before_format=self.tier_first.isChecked(),
            # radio edits and live recordings are always matched to the version asked for (no choice)
            exclude_radio_edits=True, exclude_live=True,
            exclude_mixed=self.ex_mixed.isChecked(), exclude_clean=self.ex_clean.isChecked(),
            duration_tolerance_s=self.tolerance.value(),
            skip_flagged_by_default=self.skip_flagged.isChecked(),
        )
