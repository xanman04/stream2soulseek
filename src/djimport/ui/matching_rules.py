"""The "Matching rules" editor: which files the user accepts. Embedded in the Settings dialog.

Defaults are the owner's rules. A different remix or a VIP mismatch is never accepted and has no switch.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..matching.formats import NAMES
from ..models import DEFAULT_MIN_KBPS, FORMAT_KINDS, LOSSY_KINDS, FormatRule, MatchPolicy

EXCLUSION_HELP = (
    "Switching a rejection off does not make a different recording acceptable. It only stops the label "
    "from rejecting a file: a radio edit, live recording or mixed track is then used only when the track "
    "you asked for carries that same label. A live or radio version of a normal studio track is still "
    "a different version and is still rejected."
)
NEVER_NOTE = "A different remix or remixer, a different version, and a VIP mismatch are never accepted."


class MatchingRulesWidget(QWidget):
    def __init__(self, policy: MatchPolicy, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lossy: dict[str, tuple[int, bool]] = {}
        self._current_kind: str | None = None

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

        # ---- exclusions
        self.ex_radio = QCheckBox("Reject radio edits")
        self.ex_live = QCheckBox("Reject live recordings")
        self.ex_mixed = QCheckBox("Reject [Mixed] / DJ-mixed tracks")
        ebox = QGroupBox("Rejections")
        el = QVBoxLayout(ebox)
        for w in (self.ex_radio, self.ex_live, self.ex_mixed):
            el.addWidget(w)
        help_ = QLabel(EXCLUSION_HELP)
        help_.setWordWrap(True)
        el.addWidget(help_)

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
        for w in (fbox, vbox, ebox, rbox):
            lay.addWidget(w)
        lay.addLayout(foot)
        lay.addStretch(1)

        self.formats.currentRowChanged.connect(self._row_changed)
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.restore_btn.clicked.connect(self.restore_defaults)
        self.set_policy(policy)

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
        self.ex_radio.setChecked(p.exclude_radio_edits)
        self.ex_live.setChecked(p.exclude_live)
        self.ex_mixed.setChecked(p.exclude_mixed)
        self.tolerance.setValue(p.duration_tolerance_s)
        self.skip_flagged.setChecked(p.skip_flagged_by_default)

    def restore_defaults(self) -> None:
        self.set_policy(MatchPolicy())

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
            exclude_radio_edits=self.ex_radio.isChecked(), exclude_live=self.ex_live.isChecked(),
            exclude_mixed=self.ex_mixed.isChecked(), duration_tolerance_s=self.tolerance.value(),
            skip_flagged_by_default=self.skip_flagged.isChecked(),
        )
