"""The end-of-run notice: which sources refused, and which books that cost.

It floats over the bottom-right corner of the window when the queue drains with
problems, and stays until you dismiss it - nothing about it blocks the window. Each
source says why it failed, what that meant for the requests, and where the affected
books ended up once the other sources had their turn - with buttons that select or
re-queue the books that still need a look, or all of them.
"""

from __future__ import annotations

from html import escape
from typing import Callable, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .queue_dialog import plural
from .theme import ACCENT_DARK, BG_RAISED, BORDER, STATUS_TEXT, TEXT, TEXT_DIM


class RunIssuesPanel(QFrame):
    """Non-modal, manually dismissed summary of a run's source problems."""

    select_requested = pyqtSignal(list)            # source keys
    requeue_requested = pyqtSignal(list)           # source keys
    select_ids_requested = pyqtSignal(list)        # entry ids
    requeue_ids_requested = pyqtSignal(str, list)  # source key, entry ids

    def __init__(self, provider: Callable[[], List[Dict]], parent=None):
        """`provider` returns the outstanding issues, one dict per source."""
        super().__init__(parent)
        self.provider = provider
        self.setObjectName('runIssues')
        self.setStyleSheet(
            f'#runIssues {{ background: {BG_RAISED}; border: 1px solid {ACCENT_DARK};'
            f' border-radius: 6px; }}')
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        title = QLabel('<b>Identify report: some online sources failed</b>')
        title.setStyleSheet(f'color: {STATUS_TEXT.get("risky", TEXT)};')
        layout.addWidget(title)
        intro = QLabel('The run has already finished - nothing is waiting on this. '
                       'Below is what failed, why, and what it did to the books.')
        intro.setWordWrap(True)
        intro.setStyleSheet(f'color: {TEXT_DIM};')
        layout.addWidget(intro)

        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        layout.addLayout(self.rows)

        bottom = QHBoxLayout()
        self.select_all = QPushButton('Select all affected')
        self.select_all.clicked.connect(lambda: self.select_requested.emit(self._keys))
        bottom.addWidget(self.select_all)
        self.requeue_all = QPushButton('Re-queue all')
        self.requeue_all.setToolTip('One identify job per source, for every book it '
                                    'failed or skipped')
        self.requeue_all.clicked.connect(
            lambda: self.requeue_requested.emit(self._keys))
        bottom.addWidget(self.requeue_all)
        bottom.addStretch(1)
        dismiss = QPushButton('Dismiss report')
        dismiss.setToolTip('Hide this report. Right-click an affected book > '
                           'Re-queue failed sources > Show the run summary brings '
                           'it back.')
        dismiss.clicked.connect(self.hide)
        bottom.addWidget(dismiss)
        layout.addLayout(bottom)

        self._keys: List[str] = []
        self.setFixedWidth(560)

    def refresh(self) -> None:
        """Redraw from the provider. Hides itself once nothing is outstanding."""
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()

        rows = self.provider() or []
        self._keys = [row['key'] for row in rows]
        for row in rows:
            self.rows.addWidget(self._source_box(row))

        self.select_all.setVisible(len(rows) > 1)
        self.requeue_all.setVisible(len(rows) > 1)
        if not rows:
            self.hide()
        self.adjustSize()

    def _source_box(self, row: Dict) -> QFrame:
        """One source: why it failed, what happened, what it cost, and buttons."""
        box = QFrame()
        box.setObjectName('runIssueSource')
        box.setStyleSheet(f'#runIssueSource {{ border-top: 1px solid {BORDER}; }}')
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        label = row['label']
        dim = f'<span style="color:{TEXT_DIM}">'
        lines = [f'<b>{escape(label)}</b>',
                 f'{dim}Why:</span> '
                 f'{escape(row.get("message") or "no reason was given")}']

        what = []
        if row.get('failed'):
            what.append(f'{plural(len(row["failed"]), "book")} asked, but the '
                        f'request failed')
        if row.get('skipped'):
            what.append(f'{plural(len(row["skipped"]), "book")} never asked - it '
                        f'had been switched off')
        if row.get('disabled'):
            what.append(f'{escape(label)} was switched off for the rest of that run')
        lines.append(f'{dim}What happened:</span> ' + '; '.join(what) + '.')

        missing = list(row.get('missing', []))
        doubtful = list(row.get('doubtful', []))
        fine = list(row.get('fine', []))
        threshold = f'{row.get("threshold", 0.80):.0%}'
        if not missing and not doubtful:
            colour = STATUS_TEXT.get('approved', TEXT)
            count = 'it' if len(fine) == 1 else f'all {len(fine)}'
            result = (f'No action needed. The other sources still identified '
                      f'{count} with at least {threshold} confidence.')
        else:
            colour = STATUS_TEXT.get('risky', TEXT)
            parts = []
            if missing:
                verb = 'has' if len(missing) == 1 else 'have'
                parts.append(f'{plural(len(missing), "book")} still {verb} no '
                             f'author or title')
            if doubtful:
                verb = 'is' if len(doubtful) == 1 else 'are'
                parts.append(f'{plural(len(doubtful), "book")} {verb} below '
                             f'{threshold} confidence')
            result = (f'Action needed: {" and ".join(parts)}. Check them, or '
                      f're-queue them once {escape(label)} works again.')
            if fine:
                verb = 'was' if len(fine) == 1 else 'were'
                result += (f' The other {plural(len(fine), "book")} {verb} '
                           f'identified confidently anyway.')
        lines.append(f'{dim}Result:</span> <span style="color:{colour}">{result}</span>')

        text = QLabel('<br>'.join(lines))
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setStyleSheet(f'color: {TEXT};')
        layout.addWidget(text)

        buttons = QHBoxLayout()
        weak = missing + doubtful
        affected = len(row.get('failed', [])) + len(row.get('skipped', []))
        if weak:
            select = QPushButton(f'Select the {len(weak)} to check')
            select.setToolTip(f'Select the books with no author or title, or below '
                              f'{threshold} confidence')
            select.clicked.connect(
                lambda _=False, ids=weak: self.select_ids_requested.emit(ids))
            buttons.addWidget(select)
            requeue = QPushButton(f'Re-queue those {len(weak)}')
            requeue.setToolTip(f'Identify these books again with {label} only')
            requeue.clicked.connect(
                lambda _=False, k=row['key'], ids=weak:
                self.requeue_ids_requested.emit(k, ids))
            buttons.addWidget(requeue)
        select_all = QPushButton(f'Select all {affected}')
        select_all.setToolTip('Select every book this source failed or skipped')
        select_all.clicked.connect(
            lambda _=False, k=row['key']: self.select_requested.emit([k]))
        buttons.addWidget(select_all)
        requeue_all = QPushButton(f'Re-queue all {affected}')
        requeue_all.setToolTip(f'Identify every one of these books again with '
                               f'{label} only')
        requeue_all.clicked.connect(
            lambda _=False, k=row['key']: self.requeue_requested.emit([k]))
        buttons.addWidget(requeue_all)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return box
