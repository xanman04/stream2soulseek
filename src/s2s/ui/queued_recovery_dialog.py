"""Review-only chooser for backups to recover a queued transfer that never starts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QSpinBox,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..models import Approval, SelectionItem, TransferState
from ..queue_recovery import proposed_backups
from .context import AppContext


class QueuedRecoveryDialog(QDialog):
    """Show eligible queued primary files and let the user approve reviewed backups."""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Switch stuck downloads to another uploader")
        self.resize(1050, 620)
        self.approvals: dict[str, Approval] = {}
        self._results = {r.track.apple_id: r for r in ctx.store.load_match_results(ctx.import_id)} if ctx.import_id else {}
        self._items: dict[QTreeWidgetItem, tuple[str, str, SelectionItem | None]] = {}

        self.explanation = QLabel(
            "These downloads are still waiting in an uploader's queue with nothing transferred. Tick other "
            "uploaders of the same file that S2S may switch to. If a download has not started after the time "
            "below, S2S tries a ticked one. The first that starts sending is kept and the rest are cancelled; "
            "nothing outside this list is ever tried.", self,
        )
        self.explanation.setWordWrap(True)
        self.wait_seconds = QSpinBox(self)
        self.wait_seconds.setRange(5, 300)
        self.wait_seconds.setValue(45)
        self.wait_seconds.setSuffix(" seconds")
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Track / uploader", "File on Soulseek", "Format", "Bitrate", ""])
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemChanged.connect(self._item_changed)

        top = QHBoxLayout()
        top.addWidget(QLabel("Switch when nothing has started after:", self))
        top.addWidget(self.wait_seconds)
        top.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.explanation)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        self.empty_note = QLabel("No waiting download has another uploader with the same file.", self)
        self.empty_note.setWordWrap(True)
        layout.addWidget(self.empty_note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.approve_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.approve_btn.setText("Allow ticked uploaders")
        self.approve_btn.setEnabled(False)
        buttons.accepted.connect(self._accept_if_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._populate()

    def _populate(self) -> None:
        if not self.ctx.import_id:
            self.empty_note.setText("No import is selected.")
            return
        approvals = self.ctx.store.approvals_for_import(self.ctx.import_id)
        latest: dict[str, Approval] = {}
        for approval in approvals:
            for selected in approval.items:
                latest[selected.apple_id] = approval
        transfers = {(r.approval_id, r.apple_id): r.status for r in self.ctx.store.transfers_for_import(self.ctx.import_id)}
        existing = {(p["primary_approval_id"], p["apple_id"]) for p in self.ctx.store.fallback_plans(self.ctx.import_id)}
        tracks = {t.apple_id: t for t in self.ctx.store.load_tracks(self.ctx.import_id)}

        shown = 0
        for apple_id, approval in latest.items():
            selected = next(i for i in approval.items if i.apple_id == apple_id)
            status = transfers.get((approval.id, apple_id))
            if (status is None or status.state not in (TransferState.QUEUED, TransferState.IN_PROGRESS)
                    or status.bytes_transferred != 0 or status.percent > 0 or (approval.id, apple_id) in existing):
                continue
            result = self._results.get(apple_id)
            if result is None:
                continue
            backups = proposed_backups(result, selected, limit=5)
            if not backups:
                continue
            track = tracks.get(apple_id, result.track)
            title = f"{track.name} — {track.artist} ({selected.username})"
            primary_candidate = next((c for c in ([result.best] if result.best else []) + list(result.alternatives)
                                      if c.file.username == selected.username and c.file.filename == selected.filename
                                      and c.file.size == selected.size), None)
            primary_file = primary_candidate.file if primary_candidate else None
            primary_format = primary_file.extension.upper() if primary_file and primary_file.extension else ""
            primary_bitrate = f"{primary_file.bitrate} kbps" if primary_file and primary_file.bitrate is not None else ""
            parent = QTreeWidgetItem([title, selected.filename, primary_format, primary_bitrate, "Already approved"])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            parent.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(parent)
            self._items[parent] = (approval.id, apple_id, None)
            for backup in backups:
                candidate = next((c for c in ([result.best] if result.best else []) + list(result.alternatives)
                                  if c.file.username == backup.username and c.file.filename == backup.filename
                                  and c.file.size == backup.size), None)
                file = candidate.file if candidate else None
                extension = file.extension.upper() if file and file.extension else ""
                bitrate = f"{file.bitrate} kbps" if file and file.bitrate is not None else ""
                child = QTreeWidgetItem([backup.username, backup.filename, extension, bitrate, "Other uploader"])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                parent.addChild(child)
                self._items[child] = (approval.id, apple_id, backup)
            parent.setExpanded(True)
            self.approvals[approval.id] = approval
            shown += 1
        self.empty_note.setVisible(shown == 0)
        if shown:
            self.empty_note.setText("Only queued downloads with nothing transferred are listed.")
        self._update_approve()

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        self._update_approve()

    def _update_approve(self) -> None:
        self.approve_btn.setEnabled(any(
            backup is not None
            and parent.checkState(0) != Qt.CheckState.Unchecked
            and item.checkState(0) == Qt.CheckState.Checked
            for item, (aid, apple_id, backup) in self._items.items()
            if (parent := item.parent()) is not None
        ))

    def _accept_if_selected(self) -> None:
        if self.approved_backups():
            self.accept()

    def approved_backups(self) -> dict[str, dict[str, tuple[SelectionItem, ...]]]:
        selected: dict[str, dict[str, list[SelectionItem]]] = {}
        for item, (approval_id, apple_id, backup) in self._items.items():
            parent = item.parent()
            if (backup is not None and parent is not None
                    and parent.checkState(0) != Qt.CheckState.Unchecked
                    and item.checkState(0) == Qt.CheckState.Checked):
                selected.setdefault(approval_id, {}).setdefault(apple_id, []).append(backup)
        return {aid: {apple_id: tuple(items) for apple_id, items in tracks.items()}
                for aid, tracks in selected.items()}
