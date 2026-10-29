"""Standalone native search page for a single requested track or a raw Soulseek query."""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, replace
from pathlib import PureWindowsPath

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLayout, QLineEdit, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..matching.formats import describe_policy, ext_of
from ..models import Candidate, RemoteFile, Tier
from . import theme
from .util import SORT_ROLE, fmt_mib, fmt_seconds, item, make_table
from .workers import run_async
from .icons import set_icon


_UNKNOWN_FORMAT = "Unknown"
_FILE_ROLE = SORT_ROLE + 1
_ORDER_ROLE = SORT_ROLE + 2


def _file_format(file: RemoteFile) -> str:
    return (ext_of(file) or _UNKNOWN_FORMAT).upper()


def _quality(file: RemoteFile) -> str:
    ext = _file_format(file)
    if file.bitrate is None:
        return ext
    return f"{ext} {'VBR ' if file.is_vbr else ''}{file.bitrate} kbps"


@dataclass(frozen=True)
class _Filters:
    filename: str = ""
    uploader: str = ""
    min_bitrate: int = 0
    min_duration: int = 0
    max_duration: int = 0
    min_size_mib: float = 0
    max_size_mib: float = 0
    free_slots: str = "Any"
    keep_unknown: bool = True


class _FilterBarLayout(QLayout):
    """Keep controls and active chips together, wrapping without widening the page."""

    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        rows, row, used, height = [], [], 0, 0
        for item in self._items:
            size = item.sizeHint()
            width = min(size.width(), max(1, rect.width()))
            if row and used + self.spacing() + width > rect.width():
                rows.append((row, height))
                row, used, height = [], 0, 0
            x = used + (self.spacing() if row else 0)
            row.append((item, x, width, size.height()))
            used = x + width
            height = max(height, size.height())
        if row:
            rows.append((row, height))
        y = rect.y()
        for row, height in rows:
            if apply:
                for item, x, width, h in row:
                    item.setGeometry(QRect(rect.x() + x, y + (height - h) // 2, width, h))
            y += height + self.spacing()
        return y - rect.y() - (self.spacing() if rows else 0)


class _FormatPopup(QFrame):
    def __init__(self, page):
        super().__init__(page, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.addWidget(QLabel("Formats"))
        all_formats = QPushButton("All formats")
        root.addWidget(all_formats)
        self.choices = {}
        for ext in sorted(page._available_formats | page._selected_formats):
            check = QCheckBox(ext.upper())
            check.setChecked(ext in page._selected_formats)
            check.toggled.connect(lambda checked, ext=ext: page._toggle_format(ext, checked))
            root.addWidget(check)
            self.choices[ext] = check
        if not self.choices:
            root.addWidget(theme.subtle(QLabel("Search to see available formats.")))

        def clear():
            page._selected_formats.clear()
            for check in self.choices.values():
                check.blockSignals(True)
                check.setChecked(False)
                check.blockSignals(False)
            page._apply_local_filters()

        all_formats.clicked.connect(clear)


class _FilterPopup(QFrame):
    """Anchored, nonmodal local display filters for already returned free-search results."""

    def __init__(self, current: _Filters, parent: "TrackSearchPage") -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.page = parent
        self.setFrameShape(QFrame.Shape.StyledPanel)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        heading = QLabel("Filter results")
        heading.setObjectName("sectionTitle")
        root.addWidget(heading)
        form = QFormLayout()
        root.addLayout(form)
        self.filename = QLineEdit(current.filename)
        self.uploader = QLineEdit(current.uploader)
        self.min_bitrate = QSpinBox()
        self.min_bitrate.setRange(0, 10000)
        self.min_bitrate.setSpecialValueText("Any")
        self.min_bitrate.setSuffix(" kbps")
        self.min_bitrate.setValue(current.min_bitrate)
        self.min_duration = QSpinBox()
        self.max_duration = QSpinBox()
        for control in (self.min_duration, self.max_duration):
            control.setRange(0, 100000)
            control.setSpecialValueText("Any")
            control.setSuffix(" s")
        self.min_duration.setValue(current.min_duration)
        self.max_duration.setValue(current.max_duration)
        self.min_size = QDoubleSpinBox()
        self.max_size = QDoubleSpinBox()
        for control in (self.min_size, self.max_size):
            control.setRange(0, 1000000)
            control.setDecimals(1)
            control.setSpecialValueText("Any")
            control.setSuffix(" MiB")
        self.min_size.setValue(current.min_size_mib)
        self.max_size.setValue(current.max_size_mib)
        self.free_slots = QComboBox()
        self.free_slots.addItems(["Any", "Free slots only", "No free slots"])
        self.free_slots.setCurrentText(current.free_slots)
        self.keep_unknown = QCheckBox("Keep files with unknown technical details")
        self.keep_unknown.setChecked(current.keep_unknown)
        form.addRow("Filename contains", self.filename)
        form.addRow("Uploader contains", self.uploader)
        form.addRow("Minimum bitrate", self.min_bitrate)
        form.addRow("Minimum duration", self.min_duration)
        form.addRow("Maximum duration", self.max_duration)
        form.addRow("Minimum size", self.min_size)
        form.addRow("Maximum size", self.max_size)
        form.addRow("Free slots", self.free_slots)
        form.addRow("", self.keep_unknown)
        self.error = theme.banner("", "warn")
        self.error.hide()
        root.addWidget(self.error)
        actions = QHBoxLayout()
        self.clear_button = QPushButton("Clear filters")
        self.apply_button = theme.primary(QPushButton("Apply filters"))
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        actions.addWidget(self.apply_button)
        root.addLayout(actions)
        self.clear_button.clicked.connect(self.clear_filters)
        self.apply_button.clicked.connect(self.apply_filters)

    def values(self) -> _Filters:
        return _Filters(self.filename.text().strip(), self.uploader.text().strip(), self.min_bitrate.value(),
                        self.min_duration.value(), self.max_duration.value(), self.min_size.value(),
                        self.max_size.value(), self.free_slots.currentText(), self.keep_unknown.isChecked())

    def clear_filters(self) -> None:
        self.page._filters = _Filters()
        self.filename.clear()
        self.uploader.clear()
        self.min_bitrate.setValue(0)
        self.min_duration.setValue(0)
        self.max_duration.setValue(0)
        self.min_size.setValue(0)
        self.max_size.setValue(0)
        self.free_slots.setCurrentText("Any")
        self.keep_unknown.setChecked(True)
        self.page._selected_formats.clear()
        self.page._apply_local_filters()

    def apply_filters(self) -> None:
        values = self.values()
        if ((values.max_duration and values.min_duration > values.max_duration) or
                (values.max_size_mib and values.min_size_mib > values.max_size_mib)):
            theme.set_banner(self.error, "Minimum must not be greater than maximum.", "warn")
            return
        self.page._filters = values
        self.page._apply_local_filters()
        self.close()


class TrackSearchPage(QWidget):
    """Reusable manual search surface. It emits a chosen file for review and never queues it."""

    download_requested = Signal(object, str, str, str, str)
    search_cancelled = Signal()

    def __init__(self, ctx, parent: QWidget | None = None, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self.ctx = ctx
        self._mode = "quick"
        self._busy = False
        self._generation = 0
        self._cancel_event: threading.Event | None = None
        self._files: list[RemoteFile] = []
        self._candidates: list[Candidate] = []
        self._visible_files: list[RemoteFile] = []
        self._visible_candidates: list[Candidate] = []
        self._filters = _Filters()
        self._selected_formats: set[str] = set()
        self._available_formats: set[str] = set()
        self._format_popup: _FormatPopup | None = None
        self._filter_chips: dict[str, QPushButton] = {}
        self._result_snapshot: tuple[str, str, str, str, str] | None = None
        self._result_policy: str | None = None
        self._filter_popup: _FilterPopup | None = None
        self._sort_state: tuple[int, Qt.SortOrder] | None = None

        heading = QLabel("Find a track")
        heading.setObjectName("pageTitle")
        font = heading.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        heading.setFont(font)
        heading.setVisible(not embedded)
        self.mode_tabs = QWidget(self)
        self.mode_tabs.setVisible(not embedded)
        tabs = QHBoxLayout(self.mode_tabs)
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(0)
        self.quick_tab = QPushButton("Quick search")
        self.free_tab = QPushButton("Free search")
        set_icon(self.quick_tab, "quick", 18, checked_primary=True)
        set_icon(self.free_tab, "search", 18, checked_primary=True)
        for button in (self.quick_tab, self.free_tab):
            button.setCheckable(True)
        self.quick_tab.setChecked(True)
        theme.segmented([self.quick_tab, self.free_tab])
        tabs.addWidget(self.quick_tab)
        tabs.addWidget(self.free_tab)
        tabs.addStretch(1)

        self.quick_form = QWidget(self)
        quick_row = QHBoxLayout(self.quick_form)
        quick_row.setContentsMargins(0, 0, 0, 0)
        self.title_group, self.title_input = self._labeled_input("Title", "Title")
        self.artist_group, self.artist_input = self._labeled_input("Artist", "Artist")
        self.version_group, self.version_input = self._labeled_input("Version", "Version (optional)")
        self.search_button = theme.primary(QPushButton("Search"))
        set_icon(self.search_button, "search", 18, primary=True)
        quick_row.addWidget(self.title_group, 2)
        quick_row.addWidget(self.artist_group, 2)
        quick_row.addWidget(self.version_group, 1)
        quick_row.addWidget(self.search_button)

        self.free_form = QWidget(self)
        free_row = QHBoxLayout(self.free_form)
        free_row.setContentsMargins(0, 0, 0, 0)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("Search Soulseek")
        self.query_input.setAccessibleName("Raw Soulseek search")
        self.query_input.setClearButtonEnabled(True)
        free_row.addWidget(self.query_input, 1)

        self.policy_label = theme.subtle(QLabel(""))
        self.policy_label.setWordWrap(True)
        self.policy_label.setText(describe_policy(ctx.settings.match_policy))
        self.free_tools = QWidget(self)
        self._tools_row = _FilterBarLayout(self.free_tools)
        controls = QWidget()
        tools_row = QHBoxLayout(controls)
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.addWidget(QLabel("Format"))
        self.format_filter = QPushButton("All formats")
        self.format_filter.setAccessibleName("Choose formats")
        tools_row.addWidget(self.format_filter)
        self.filters_button = QPushButton("Filters")
        set_icon(self.filters_button, "filter", 16)
        tools_row.addWidget(self.filters_button)
        self._tools_row.addWidget(controls)

        self.result_count = theme.subtle(QLabel("No results"))
        self.result_count.setObjectName("resultSummary")
        self.status_label = QLabel("")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.empty_label = QLabel("Search for a track to see results.")
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        theme.subtle(self.empty_label)
        self.table = make_table(["Filename", "Uploader", "Format", "Quality", "Length", "Size (MiB)", "Availability", "Match"])
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(self.table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.sectionClicked.connect(self._sort_clicked)
        self.table.setColumnHidden(2, True)  # Quality already includes the format.
        for col, width in ((1, 145), (3, 120), (4, 65), (5, 85), (6, 100), (7, 185)):
            self.table.setColumnWidth(col, width)
        self.table.setWordWrap(False)
        if embedded:
            # The repair panel is a narrow column: the filename must stay readable, so the table keeps only the file
            # and who has it; length, size and availability are in the details line under the table.
            for col in (4, 5, 6):
                self.table.setColumnHidden(col, True)
            self.table.setColumnWidth(1, 110)
            self.table.setColumnWidth(3, 100)
            self.table.setMinimumHeight(170)
            header.setMinimumSectionSize(60)
            self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details = theme.subtle(QLabel(""))
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.review_button = theme.primary(QPushButton("Review selection…"))
        self.review_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel search")
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Soulseek search in progress")
        self.progress.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(0, 0, 0, 0) if embedded else (28, 26, 28, 18))
        layout.setSpacing(8 if embedded else 16)
        layout.addWidget(heading)
        query_panel = QFrame()
        query_panel.setObjectName("embeddedSearchControls" if embedded else "searchControls")
        query_layout = QVBoxLayout(query_panel)
        query_layout.setContentsMargins(*(0, 0, 0, 0) if embedded else (18, 16, 18, 16))
        query_layout.setSpacing(14)
        query_layout.addWidget(self.mode_tabs)
        query_layout.addWidget(self.quick_form)
        query_layout.addWidget(self.free_form)
        query_layout.addWidget(self.policy_label)
        layout.addWidget(query_panel)
        layout.addWidget(self.free_tools)
        layout.addWidget(self.result_count)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress)
        self._results_stack = QStackedWidget()
        self._results_stack.addWidget(self.table)
        self._results_stack.addWidget(self.empty_label)
        layout.addWidget(self._results_stack, 1)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.review_button)
        layout.addLayout(actions)

        self.quick_tab.clicked.connect(lambda: self.set_mode("quick"))
        self.free_tab.clicked.connect(lambda: self.set_mode("free"))
        self.search_button.clicked.connect(self.search)
        self.title_input.returnPressed.connect(self.search)
        self.artist_input.returnPressed.connect(self.search)
        self.version_input.returnPressed.connect(self.search)
        self.query_input.returnPressed.connect(self.search)
        self.cancel_button.clicked.connect(self.request_cancel)
        self.review_button.clicked.connect(self.review_selection)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self.review_selection())
        self.filters_button.clicked.connect(self._open_filters)
        self.format_filter.clicked.connect(self._open_formats)
        for field in (self.title_input, self.artist_input, self.version_input, self.query_input):
            field.textChanged.connect(lambda _text: self._update_selection())
        self.set_mode("free" if embedded else "quick")

    @staticmethod
    def _labeled_input(label_text: str, placeholder: str) -> tuple[QWidget, QLineEdit]:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        label = QLabel(label_text)
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setAccessibleName(label_text)
        label.setBuddy(field)
        column.addWidget(label)
        column.addWidget(field)
        return wrapper, field

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("quick", "free"):
            raise ValueError("mode must be 'quick' or 'free'")
        if mode == self._mode and self._generation:
            return
        self._invalidate()
        self._mode = mode
        self.table.setColumnHidden(7, mode == "free")
        self.quick_tab.setChecked(mode == "quick")
        self.free_tab.setChecked(mode == "free")
        self.quick_form.setVisible(mode == "quick")
        self.free_form.setVisible(mode == "free")
        quick_row = self.quick_form.layout()
        free_row = self.free_form.layout()
        if mode == "quick":
            free_row.removeWidget(self.search_button)
            quick_row.addWidget(self.search_button)
        else:
            quick_row.removeWidget(self.search_button)
            free_row.addWidget(self.search_button)
        self.policy_label.setVisible(mode == "quick")
        self.free_tools.setVisible(mode == "free")
        self.search_button.setVisible(True)
        self._files.clear()
        self._candidates.clear()
        self._result_snapshot = None
        self._fill_table()
        self._say("Search for a track to see results." if mode == "quick" else "Search to see returned files.")
        self._update_selection()

    def search(self) -> None:
        if self._busy:
            self.cancel()
        mode = self._mode
        title, artist, version = self.title_input.text().strip(), self.artist_input.text().strip(), self.version_input.text().strip()
        query = self.query_input.text().strip()
        if mode == "quick" and (not title or not artist):
            self._say("Enter both a title and artist to search.")
            return
        if mode == "free" and not query:
            self._say("Enter search words.")
            return
        self._generation += 1
        generation = self._generation
        snapshot = (mode, title, artist, version, query)
        policy_snapshot = self.ctx.settings.match_policy.fingerprint()
        self._result_snapshot = None
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._set_busy(True)
        self._files.clear()
        self._candidates.clear()
        self._fill_table()
        self._say("Searching…")

        def work():
            backend = importlib.import_module("s2s.track_search")
            if mode == "quick":
                return "quick", backend.quick_search(self.ctx, title, artist, version, cancel=cancel_event)
            return "free", backend.free_search(self.ctx, query, cancel=cancel_event)

        def done(result) -> None:
            if generation != self._generation or mode != self._mode:
                return
            self._set_busy(False)
            kind, rows = result
            self._result_snapshot = snapshot
            self._result_policy = policy_snapshot
            if kind == "quick":
                self._candidates = list(rows or [])
                self._files = [candidate.file for candidate in self._candidates]
            else:
                self._files = list(rows or [])
                self._candidates = []
            self._sort_state = None
            self._update_format_choices()
            self._fill_table()
            self._say("" if self._files else "No files were returned.")

        def failed(exc) -> None:
            if generation != self._generation or mode != self._mode:
                return
            self._set_busy(False)
            self._files.clear()
            self._candidates.clear()
            self._fill_table()
            self._say(self.ctx.describe(exc))

        run_async(self, work, done, failed)

    def ask_cancel(self) -> bool:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(QMessageBox.Icon.Question, "Cancel this search?",
                          "Stop searching? Existing downloads and saved transfers will keep running.", parent=self)
        stop = box.addButton("Cancel search", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Keep searching", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return box.clickedButton() is stop

    def request_cancel(self) -> None:
        if self._busy and self.ask_cancel():
            self.cancel()
            self.search_cancelled.emit()

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._busy:
            self._generation += 1
            self._set_busy(False)
            self._say("Search cancelled.")

    def _invalidate(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._generation += 1
        self._set_busy(False)

    def hideEvent(self, event) -> None:  # noqa: N802
        if self._filter_popup is not None:
            self._filter_popup.close()
        if self._format_popup is not None:
            self._format_popup.close()
        if self._busy:
            self._invalidate()
        super().hideEvent(event)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.progress.setVisible(busy)
        self.search_button.setEnabled(not busy)
        self.review_button.setEnabled(not busy and self._selected_file() is not None and self._snapshot_is_current())
        if self._embedded:
            self.query_input.setEnabled(not busy)
            self.table.setEnabled(not busy)

    def _say(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))
        self._results_stack.setCurrentWidget(self.table if self._visible_files else self.empty_label)
        if not self._files:
            self.empty_label.setText(message or ("Search for a track to see results." if self._mode == "quick"
                                                  else "Search to see returned files."))

    def _update_format_choices(self) -> None:
        self._available_formats = {_file_format(file).casefold() for file in self._files}
        if self._format_popup is not None:
            self._format_popup.close()
        self._refresh_filter_chips()

    def _open_formats(self) -> None:
        if self._format_popup is not None:
            self._format_popup.close()
        popup = _FormatPopup(self)
        self._format_popup = popup
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        popup.destroyed.connect(lambda: setattr(self, "_format_popup", None)
                                if self._format_popup is popup else None)
        popup.adjustSize()
        popup.move(self.format_filter.mapToGlobal(QPoint(0, self.format_filter.height())))
        popup.show()

    def _toggle_format(self, ext: str, checked: bool) -> None:
        if checked:
            self._selected_formats.add(ext)
        else:
            self._selected_formats.discard(ext)
        self._apply_local_filters()

    def _refresh_filter_chips(self) -> None:
        self.format_filter.setText("All formats" if not self._selected_formats else
                                   f"{len(self._selected_formats)} selected")
        for button in self._filter_chips.values():
            self._tools_row.removeWidget(button)
            button.hide()
            button.deleteLater()
        self._filter_chips.clear()
        f = self._filters
        entries = [("format:" + ext, ext.upper()) for ext in sorted(self._selected_formats)]
        entries += [(key, label) for key, value, label in (
            ("filename", f.filename, f"Filename: {f.filename}"),
            ("uploader", f.uploader, f"Uploader: {f.uploader}"),
            ("min_bitrate", f.min_bitrate, f"Bitrate ≥ {f.min_bitrate} kbps"),
            ("min_duration", f.min_duration, f"Length ≥ {fmt_seconds(f.min_duration)}"),
            ("max_duration", f.max_duration, f"Length ≤ {fmt_seconds(f.max_duration)}"),
            ("min_size_mib", f.min_size_mib, f"Size ≥ {f.min_size_mib:g} MiB"),
            ("max_size_mib", f.max_size_mib, f"Size ≤ {f.max_size_mib:g} MiB"),
            ("free_slots", f.free_slots != "Any", f.free_slots),
            ("keep_unknown", not f.keep_unknown, "Exclude unknown details"),
        ) if value]
        for key, label in entries:
            button = QPushButton()
            button.setText(button.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, 220).replace("&", "&&"))
            button.setAccessibleName(f"Remove filter: {label}")
            set_icon(button, "close", 16)
            button.setStyleSheet("QPushButton {" + theme.chip_style("info") +
                                " min-height: 20px; padding: 4px 9px; }"
                                " QPushButton:hover, QPushButton:focus { border-color: palette(highlight); }")
            button.clicked.connect(lambda _checked=False, key=key: self._remove_filter(key))
            self._filter_chips[key] = button
            self._tools_row.addWidget(button)
        self.free_tools.updateGeometry()

    def _remove_filter(self, key: str) -> None:
        if key.startswith("format:"):
            self._selected_formats.discard(key.split(":", 1)[1])
        else:
            self._filters = replace(self._filters, **{key: getattr(_Filters(), key)})
        self._apply_local_filters()

    def _open_filters(self) -> None:
        if self._filter_popup is not None:
            self._filter_popup.close()
        popup = _FilterPopup(self._filters, self)
        self._filter_popup = popup
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        popup.destroyed.connect(lambda: setattr(self, "_filter_popup", None)
                                if self._filter_popup is popup else None)
        popup.adjustSize()
        popup.move(self.filters_button.mapToGlobal(QPoint(0, self.filters_button.height())))
        popup.show()

    def _matches_filter(self, file: RemoteFile) -> bool:
        f = self._filters
        filename = file.filename.casefold()
        uploader = file.username.casefold()
        if f.filename and f.filename.casefold() not in filename:
            return False
        if f.uploader and f.uploader.casefold() not in uploader:
            return False
        ext = _file_format(file)
        if self._selected_formats and ext.casefold() not in self._selected_formats:
            return False
        if file.bitrate is None:
            if f.min_bitrate and not f.keep_unknown:
                return False
        elif file.bitrate < f.min_bitrate:
            return False
        if file.length_s is None:
            if (f.min_duration or f.max_duration) and not f.keep_unknown:
                return False
        elif ((f.min_duration and file.length_s < f.min_duration)
              or (f.max_duration and file.length_s > f.max_duration)):
            return False
        if file.size is None:
            if (f.min_size_mib or f.max_size_mib) and not f.keep_unknown:
                return False
        else:
            size_mib = file.size / (1024 * 1024)
            if ((f.min_size_mib and size_mib < f.min_size_mib)
                    or (f.max_size_mib and size_mib > f.max_size_mib)):
                return False
        if f.free_slots == "Free slots only" and file.has_free_slot is not True:
            return False
        if f.free_slots == "No free slots" and file.has_free_slot is not False:
            return False
        return True

    def _apply_local_filters(self, *_args) -> None:
        self._refresh_filter_chips()
        if self._mode == "free":
            self._fill_table()

    def _fill_table(self) -> None:
        self.table.setRowCount(0)
        if self._mode == "quick":
            self._visible_candidates = list(self._candidates)
            self._visible_files = [candidate.file for candidate in self._visible_candidates]
        else:
            self._visible_files = [file for file in self._files if self._matches_filter(file)]
            self._visible_candidates = []
        self.result_count.setText(f"{len(self._files)} returned · {len(self._visible_files)} shown")
        self._results_stack.setCurrentWidget(self.table if self._visible_files else self.empty_label)
        if not self._visible_files:
            self.empty_label.setText("No files match these filters." if self._files else
                                     ("Search for a track to see results." if self._mode == "quick"
                                      else "Search to see returned files."))
        for row, file in enumerate(self._visible_files):
            self.table.insertRow(row)
            candidate = self._visible_candidates[row] if self._mode == "quick" else None
            match = self._candidate_status(candidate) if candidate else ""
            slot = "Free slot" if file.has_free_slot is True else f"Queue: {file.queue_length}" if file.queue_length is not None else "Queued" if file.has_free_slot is False else "Unknown"
            values = [PureWindowsPath(file.filename).name, file.username, _file_format(file), _quality(file),
                      fmt_seconds(file.length_s), fmt_mib(file.size), slot, match]
            sort_values = [file.filename.casefold(), file.username.casefold(), _file_format(file).casefold(),
                           (file.bitrate if file.bitrate is not None else -1),
                           (file.length_s if file.length_s is not None else -1), file.size, slot.casefold(), match.casefold()]
            for col, (text, sort_value) in enumerate(zip(values, sort_values)):
                cell = item(text)
                cell.setData(SORT_ROLE, sort_value)
                cell.setData(_FILE_ROLE, file)
                cell.setData(_ORDER_ROLE, row)
                self.table.setItem(row, col, cell)
            if candidate is not None:
                kind = "warn" if candidate.flagged else "ok" if candidate.tier in (Tier.EXACT, Tier.EXTENDED_FALLBACK) else "error"
                for col in range(self.table.columnCount()):
                    self.table.item(row, col).setBackground(theme.tint(kind, 26))
        if self._sort_state is not None:
            self._sort_rows()
        self._update_selection()

    @staticmethod
    def _candidate_status(candidate: Candidate) -> str:
        if candidate.flagged:
            detail = next((reason for reason in candidate.reasons if "uploader reliability" not in reason.lower()), "Needs a closer look")
            return "Check: " + detail
        if candidate.tier == Tier.EXACT:
            return "Exact version"
        if candidate.tier == Tier.EXTENDED_FALLBACK:
            return "Extended mix fallback"
        return "Does not meet matching rules"

    def _sort_clicked(self, column: int) -> None:
        if self._sort_state is None or self._sort_state[0] != column:
            self._sort_state = (column, Qt.SortOrder.AscendingOrder)
        elif self._sort_state[1] == Qt.SortOrder.AscendingOrder:
            self._sort_state = (column, Qt.SortOrder.DescendingOrder)
        else:
            self._sort_state = None
        self._sort_rows()
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        header.setSortIndicator(*(self._sort_state or (-1, Qt.SortOrder.AscendingOrder)))
        header.blockSignals(False)

    def _sort_rows(self) -> None:
        selected = self._selected_file()
        rows = []
        for row in range(self.table.rowCount()):
            key = (self.table.item(row, 0).data(_ORDER_ROLE) if self._sort_state is None else
                   self.table.item(row, self._sort_state[0]).data(SORT_ROLE))
            rows.append((key, row))
        reverse = self._sort_state is not None and self._sort_state[1] == Qt.SortOrder.DescendingOrder
        rows.sort(key=lambda entry: entry[0], reverse=reverse)
        snapshots = [[self.table.item(old_row, col).clone() for col in range(self.table.columnCount())]
                     for _key, old_row in rows]
        self.table.setSortingEnabled(False)
        for row, values in enumerate(snapshots):
            for col, cell in enumerate(values):
                self.table.setItem(row, col, cell)
        if selected is not None:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(_FILE_ROLE) == selected:
                    self.table.selectRow(row)
                    break
        self._update_selection()

    def _selected_file(self) -> RemoteFile | None:
        row = self.table.currentRow()
        if not 0 <= row < self.table.rowCount():
            return None
        return self.table.item(row, 0).data(_FILE_ROLE)

    def _update_selection(self) -> None:
        row = self.table.currentRow()
        enabled = not self._busy and 0 <= row < self.table.rowCount() and self._snapshot_is_current()
        self.review_button.setEnabled(enabled)
        stale_message = "Search terms or matching rules changed. Search again before reviewing these results."
        if self._result_snapshot is not None and not self._snapshot_is_current():
            self._say(stale_message)
        elif self.status_label.text() == stale_message:
            self._say("")
        if not enabled:
            self.details.setText("")
            self.details.hide()
            return
        self.details.show()
        file = self._selected_file()
        if file is None:
            return
        candidate = next((c for c in self._visible_candidates if c.file == file), None)
        lines = [file.filename, f"{file.username} · {_quality(file)} · {fmt_seconds(file.length_s) or 'Length unknown'} · {fmt_mib(file.size)} MiB"]
        if candidate:
            lines.insert(0, self._candidate_status(candidate))
            lines.extend(candidate.reasons)
        self.details.setText("\n".join(lines))

    def _current_snapshot(self) -> tuple[str, str, str, str, str]:
        return (self._mode, self.title_input.text().strip(), self.artist_input.text().strip(),
                self.version_input.text().strip(), self.query_input.text().strip())

    def _snapshot_is_current(self) -> bool:
        return (self._result_snapshot is not None and self._result_snapshot == self._current_snapshot()
                and (self._mode == "free" or self._result_policy == self.ctx.settings.match_policy.fingerprint()))

    def review_selection(self) -> None:
        file = self._selected_file()
        if file is None or self._busy or not self._snapshot_is_current():
            return
        mode, title, artist, version, query = self._result_snapshot
        self.download_requested.emit(file, mode, title if mode == "quick" else query,
                                     artist if mode == "quick" else "", version if mode == "quick" else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self._embedded or self.width() < 900  # the repair panel is always a narrow column
        self.table.setColumnHidden(5, compact)
        self.table.setColumnHidden(6, compact)
        if self._embedded:
            self.table.setColumnHidden(4, True)
        self.table.setColumnWidth(0, max(230, self.table.columnWidth(0)))
