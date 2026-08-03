"""The job queue, as a window you can watch and act on.

A count in the corner of the toolbar tells you how long to wait and nothing else. What
you actually want to know when three identifications are stacked up is which book is
being worked on right now, how far into it we are, and how far through the whole batch
we are - and then to be able to pull one job out, or bin the lot.

So the layout is fixed and reads top to bottom:

    Queue                 <- bold, underlined; the title of the thing
    3 jobs waiting        <- the count, in words that agree with the number
    Identifying "..."     <- the job running right now, with its own progress bar
    [========      ]
    Whole queue           <- every job accepted since the queue was last empty
    [====          ]
    1. ...  2. ...        <- what is behind it, each removable

The window stays open while jobs drain and is refreshed by the controller, so adding
more work while watching it extends the lower bar rather than resetting anything.
"""

from __future__ import annotations

from typing import Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .theme import ACCENT, BG_BASE, BORDER, TEXT, TEXT_DIM, TEXT_FAINT


def plural(count: int, singular: str, many: str = '') -> str:
    """"1 job" / "2 jobs" - never "1 job(s)"."""
    return f'{count} {singular if count == 1 else (many or singular + "s")}'


class QueueDialog(QDialog):
    """Live view of the running job and everything behind it."""

    remove_requested = pyqtSignal(int)
    clear_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Queue')
        self.resize(640, 460)
        # Not modal: the whole point is to keep working while jobs drain.
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        title = QLabel('Queue')
        title.setStyleSheet(
            f'color: {TEXT}; font-size: 16px; font-weight: 700; '
            f'text-decoration: underline; padding-bottom: 2px;')
        layout.addWidget(title)

        self.count_label = QLabel('')
        self.count_label.setStyleSheet(f'color: {TEXT_DIM}; padding-bottom: 6px;')
        layout.addWidget(self.count_label)

        layout.addWidget(self._rule())

        self.current_label = QLabel('')
        self.current_label.setWordWrap(True)
        self.current_label.setStyleSheet(f'color: {TEXT}; padding-top: 6px;')
        layout.addWidget(self.current_label)

        self.current_bar = QProgressBar()
        self.current_bar.setFixedHeight(16)
        self.current_bar.setToolTip('Progress through the job running right now')
        layout.addWidget(self.current_bar)

        overall = QLabel('Whole queue')
        overall.setStyleSheet(
            f'color: {TEXT_DIM}; font-weight: 600; padding-top: 10px;')
        layout.addWidget(overall)

        self.overall_bar = QProgressBar()
        self.overall_bar.setFixedHeight(16)
        self.overall_bar.setToolTip(
            'Jobs finished out of every job accepted since the queue was last empty. '
            'Adding more work extends this bar rather than restarting it.')
        layout.addWidget(self.overall_bar)

        waiting = QLabel('Waiting')
        waiting.setStyleSheet(
            f'color: {TEXT_DIM}; font-weight: 600; padding-top: 10px;')
        layout.addWidget(waiting)

        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_area.setFrameShape(QFrame.Shape.NoFrame)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(4)
        self.list_layout.addStretch(1)
        self.list_area.setWidget(self.list_host)
        layout.addWidget(self.list_area, stretch=1)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('Cancel the running job')
        self.cancel_button.setProperty('danger', True)
        self.cancel_button.setToolTip(
            'Stop the job running right now. Everything waiting behind it stays in '
            'the queue and starts next.')
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        buttons.addWidget(self.cancel_button)

        self.clear_button = QPushButton('Clear everything waiting')
        self.clear_button.setToolTip(
            'Drop every queued job. The one already running is left alone.')
        self.clear_button.clicked.connect(self.clear_requested.emit)
        buttons.addWidget(self.clear_button)

        buttons.addStretch(1)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.update_status({}, 0, 0, '')

    @staticmethod
    def _rule() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f'color: {BORDER}; background: {BORDER}; max-height: 1px;')
        return line

    # ------------------------------------------------------------------ update

    def update_status(self, status: Dict, done: int = 0, total: int = 0,
                      message: str = '') -> None:
        """Redraw from a WorkerManager.status() snapshot plus the live progress.

        `done`/`total`/`message` are the running job's own progress - the same numbers
        the toolbar bar shows - so the two never disagree.
        """
        queued: List[str] = list(status.get('queued') or [])
        running = status.get('running')

        waiting = len(queued)
        if running:
            self.count_label.setText(
                f'Running 1 job, {plural(waiting, "job")} waiting behind it'
                if waiting else 'Running 1 job, nothing waiting behind it')
        else:
            self.count_label.setText('Nothing is running')

        self.current_label.setText(
            f'<span style="color:{ACCENT}">{message or running}</span>'
            if (running or message) else
            f'<span style="color:{TEXT_FAINT}">Idle</span>')

        if running and total > 0:
            # `done`/`total` may be fractional - a chapter merge reports progress from
            # inside a single chapter. So the bar runs on a fixed 1000-step range and is
            # filled from the exact fraction, and the text counts in whole units. Same
            # scheme as MainWindow.show_progress, so the two bars never disagree.
            fraction = max(0.0, min(1.0, done / total))
            self.current_bar.setRange(0, 1000)
            self.current_bar.setValue(int(round(fraction * 1000)))
            self.current_bar.setFormat(
                f'{int(done)} / {int(total)}  ({fraction:.0%})')
        elif running:
            self.current_bar.setRange(0, 0)      # running, but no count yet
            self.current_bar.setFormat('')
        else:
            self.current_bar.setRange(0, 1)
            self.current_bar.setValue(0)
            self.current_bar.setFormat('')

        accepted = int(status.get('accepted') or 0)
        completed = int(status.get('completed') or 0)
        self.overall_bar.setRange(0, max(1, accepted))
        self.overall_bar.setValue(min(completed, accepted))
        self.overall_bar.setFormat(
            f'{completed} of {plural(accepted, "job")} done' if accepted
            else 'nothing queued')

        self._fill_list(queued)
        self.cancel_button.setEnabled(bool(running))
        self.clear_button.setEnabled(bool(queued))

    def _fill_list(self, queued: List[str]) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not queued:
            empty = QLabel('Nothing is waiting.')
            empty.setStyleSheet(f'color: {TEXT_FAINT};')
            self.list_layout.addWidget(empty)
        for index, label in enumerate(queued):
            self.list_layout.addWidget(self._job_row(index, label))
        self.list_layout.addStretch(1)

    def _job_row(self, index: int, label: str) -> QWidget:
        row = QWidget()
        row.setStyleSheet(f'background: {BG_BASE}; border: 1px solid {BORDER}; '
                          f'border-radius: 5px;')
        inner = QHBoxLayout(row)
        inner.setContentsMargins(10, 6, 6, 6)
        inner.setSpacing(8)

        position = QLabel(f'{index + 1}.')
        position.setStyleSheet(f'color: {TEXT_FAINT}; border: none;')
        inner.addWidget(position)

        name = QLabel(label)
        name.setWordWrap(True)
        name.setStyleSheet(f'color: {TEXT}; border: none;')
        inner.addWidget(name, stretch=1)

        remove = QPushButton('Remove')
        remove.setToolTip('Take this job out of the queue')
        remove.setFixedWidth(90)
        remove.clicked.connect(
            lambda _=False, i=index: self.remove_requested.emit(i))
        inner.addWidget(remove)
        return row
