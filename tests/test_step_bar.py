"""The step bar (the playlist steps along the top of each workflow page) and the shared page header."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from s2s.ui.page_header import PageHeader
from s2s.ui.steps import STEPS, StepBar, StepFrame


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


DONE = [True, True, False, False, False, False]
OPEN = [True, True, True, False, False, False]


def test_each_step_is_marked_current_done_or_not_yet_available(qapp):
    bar = StepBar(2)
    bar.set_states(DONE, OPEN)
    nodes = bar.nodes
    assert len(nodes) == len(STEPS)
    assert nodes[2].current and not nodes[2].done
    assert [n.isEnabled() for n in nodes] == [True, True, True, False, False, False]
    assert "current" in nodes[2].accessibleName() and "Find on Soulseek" in nodes[2].accessibleName()
    assert "done" in nodes[0].accessibleName()
    assert "not available" in nodes[3].accessibleName()
    assert all(not n.toolTip() for n in nodes)  # no hover tooltips anywhere in S2S
    for n in nodes:
        assert not n.grab().isNull()  # every state paints
    bar.deleteLater()


def test_the_current_step_stays_enabled_even_when_the_run_has_closed_it(qapp):
    bar = StepBar(4)  # once downloads are confirmed the earlier steps are closed, but the page itself is open
    bar.set_states([True] * 5 + [False], [False, False, False, False, True, True])
    assert [n.isEnabled() for n in bar.nodes] == [False, False, False, False, True, True]
    bar.deleteLater()


def test_clicking_another_open_step_asks_to_go_there(qapp):
    bar = StepBar(2)
    bar.set_states(DONE, OPEN)
    asked = []
    bar.step_requested.connect(asked.append)
    bar.nodes[0].click()
    bar.nodes[2].click()  # this page: nothing to do
    bar.nodes[3].click()  # not open yet: a disabled button ignores the click
    assert asked == [0]
    bar.deleteLater()


def test_the_bar_drops_the_step_names_before_it_would_crowd_the_title(qapp):
    bar = StepBar(0)
    bar.resize(520, bar.height())
    bar.show()
    QApplication.processEvents()
    assert bar.labels_shown()
    bar.resize(300, bar.height())
    QApplication.processEvents()
    assert not bar.labels_shown()
    assert bar.minimumSizeHint().width() <= 300
    bar.close()
    bar.deleteLater()


def test_the_header_keeps_one_height_at_every_width_so_pages_lose_no_space(qapp):
    frame = StepFrame(4, QWidget())
    heights = set()
    for width in (1400, 1100, 900):
        frame.resize(width, 600)
        frame.show()
        QApplication.processEvents()
        heights.add(frame.header.height())
        assert frame.title_label.text() == "Downloads & import"
    assert len(heights) == 1
    # The title and the bar share one row: the header is about as tall as the title alone, plus its rule.
    assert frame.header.height() <= frame.title_label.sizeHint().height() + frame.step_bar.height()
    frame.close()
    frame.deleteLater()


def test_the_frame_passes_a_step_request_up_and_keeps_its_bar_in_sync(qapp):
    frame = StepFrame(1, QWidget())
    asked = []
    frame.step_requested.connect(asked.append)
    frame.set_steps(DONE, OPEN)
    frame.step_bar.nodes[0].click()
    assert asked == [0]
    assert frame.step_bar.current == 1
    frame.deleteLater()


def test_page_header_shows_a_subtitle_only_when_there_is_one(qapp):
    plain = PageHeader("Title")
    assert plain.subtitle_label.isHidden()
    described = PageHeader("Title", "What this page is for.")
    assert not described.subtitle_label.isHidden() and described.subtitle_label.text() == "What this page is for."
    described.set_subtitle("")
    assert described.subtitle_label.isHidden()
    for header in (plain, described):
        assert header.rule.height() == 1
        header.deleteLater()
