"""The "why" panel (#29) - how a given entry came to be identified the way it is.

One card per thing that happened, each collapsible and each remembering whether you
left it open. The language-model card carries the full exchange - system prompt, the
exact question asked, the raw reply and the parsed result - because when an
identification is wrong, the reply is the only thing that explains it.

A library card sits at the top and is driven by the whole entry set rather than the
selection, so it stays current after a scan without anything being selected.

Everything here is rendered in neutral greys. The one exception is the review status,
which is the only thing in the application that colour is allowed to mean.
"""

from __future__ import annotations

import html
import json
from typing import Dict, Iterable, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from ..models import IDENTITY_FIELDS, BookEntry
from ..settings import display_path
from .theme import (ACCENT, BG_BASE, BG_DARKEST, BG_HOVER, BG_RAISED, BORDER,
                    STATUS_HUES, STATUS_TEXT, TEXT, TEXT_DIM, TEXT_FAINT,
                    confidence_color, source_color)

# How well a source did, and the colour that says so. This is the only thing on the
# panel besides the review status that colour is allowed to mean, and it means one
# thing: did asking this source get us anything usable?
#
#   GOOD     everything this source could have told us, it told us
#   PARTIAL  it answered, but the answer is incomplete or was not good enough to use
#   EMPTY    it ran and came back with nothing
#   NOT_RUN  it was never asked
GOOD, PARTIAL, EMPTY, NOT_RUN = 'good', 'partial', 'empty', 'not_run'

STATE_COLOURS = {
    GOOD: STATUS_HUES['approved'],
    PARTIAL: STATUS_HUES['risky'],
    EMPTY: STATUS_HUES['rejected'],
    NOT_RUN: TEXT_FAINT,
}

# The fields a source is judged against. A book with no series is complete without a
# series index, so completeness is judged per book rather than as "all four".
JUDGED_FIELDS = ('author', 'title', 'series', 'series_index')

# The sources that always get a card, in the order the resolver runs them.
SOURCE_ORDER = ['metadata', 'regex', 'api', 'search', 'llm']

TIER_LABELS = {
    'metadata': 'Embedded tags',
    'regex': 'Filename parsing',
    'api': 'Book databases',
    'search': 'Web search',
    'llm': 'Language model',
    'folder': 'Folder siblings',
    'dedupe': 'Duplicate check',
    'quality': 'Looks wrong',
    'auto': 'Auto-approval',
    'user': 'Manual edit',
    'cancelled': 'Cancelled',
}

# Cards open by default. The LLM exchange is long, so it starts collapsed.
DEFAULT_OPEN = {'library', 'summary', 'fields', 'metadata', 'regex', 'api', 'search',
                'llm', 'folder', 'dedupe', 'quality', 'auto', 'user'}


class CollapsibleCard(QWidget):
    """A titled box that folds away when you click its header.

    Source cards also carry a Run button in that header row: the panel is where you
    look when a value is wrong, so it is where re-asking one source belongs.

    ``body`` is either rich text or a whole widget. The widget form is what lets the
    book-database card hold one sub-card per database instead of concatenating five
    databases' results into a single wall of text.
    """

    def __init__(self, key: str, title: str, body, badge: str = '',
                 colour: str = ACCENT, expanded: bool = True, panel=None, parent=None,
                 run: str = '', has_run: bool = False, run_key: str = ''):
        super().__init__(parent)
        self.key = key
        self.panel = panel
        # What Run asks for, when that is not simply this card's key. A database's own
        # card is keyed "api::itunes" for its fold state but has to request "api:itunes",
        # which is the form the resolver reads as "the api tier, this source only".
        self.run_key = run_key or key

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        self.header = QPushButton()
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setStyleSheet(f"""
            QPushButton {{
                background: {BG_RAISED}; border: 1px solid {BORDER};
                border-top-left-radius: 5px; border-top-right-radius: 5px;
                border-bottom-left-radius: 0; border-bottom-right-radius: 0;
                padding: 7px 10px; text-align: left;
                color: {colour}; font-size: 11px; font-weight: 700;
                letter-spacing: 0.5px;
            }}
            QPushButton:hover {{ background: {BG_HOVER}; }}
            QPushButton:!checked {{
                border-bottom-left-radius: 5px; border-bottom-right-radius: 5px;
            }}
        """)
        self.header.clicked.connect(self._toggle)
        header_row.addWidget(self.header, stretch=1)

        if run:
            # White until this source has actually run for this book, then green.
            # The button used to be accent-coloured whatever had happened, so it read
            # as a warning rather than as "this one is still untouched".
            tint = STATUS_HUES['approved'] if has_run else TEXT
            self.run_button = QPushButton('Run again' if has_run else 'Run')
            self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.run_button.setToolTip(
                f'{run} has already run for this book - run it again'
                if has_run else f'Ask {run} about this book now')
            self.run_button.setStyleSheet(f"""
                QPushButton {{
                    background: {BG_RAISED}; border: 1px solid {BORDER};
                    border-left: none; border-top-right-radius: 5px;
                    border-bottom-right-radius: 5px; border-top-left-radius: 0;
                    border-bottom-left-radius: 0;
                    padding: 7px 12px; color: {tint}; font-weight: 700;
                    font-size: 11px;
                }}
                QPushButton:hover {{ background: {tint}; color: {BG_DARKEST}; }}
            """)
            self.run_button.clicked.connect(
                lambda: self.panel.run_requested.emit(self.run_key)
                if self.panel is not None else None)
            header_row.addWidget(self.run_button)

        layout.addLayout(header_row)

        frame = (f'background: {BG_BASE}; border: 1px solid {BORDER};'
                 f' border-top: none; border-bottom-left-radius: 5px;'
                 f' border-bottom-right-radius: 5px; padding: 8px; color: {TEXT};')
        if isinstance(body, QWidget):
            self.body = body
            self.body.setObjectName('cardBody')
            # Scoped to the object name, or every child label inherits the frame and
            # the card fills with nested boxes.
            self.body.setStyleSheet(f'QWidget#cardBody {{ {frame} }}')
        else:
            self.body = QLabel(body)
            self.body.setTextFormat(Qt.TextFormat.RichText)
            self.body.setWordWrap(True)
            self.body.setOpenExternalLinks(True)
            self.body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.body.setAlignment(Qt.AlignmentFlag.AlignTop
                                   | Qt.AlignmentFlag.AlignLeft)
            self.body.setStyleSheet(f'QLabel {{ {frame} }}')
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Minimum)
        layout.addWidget(self.body)

        self._title = title
        self._badge = badge
        self._sync()

    # The header text carries the fold arrow, so no extra icon assets are needed.
    def _sync(self) -> None:
        arrow = '▾' if self.header.isChecked() else '▸'
        self.header.setText(f'{arrow}  {self._title}'
                            + (f'   ·   {self._badge}' if self._badge else ''))
        self.body.setVisible(self.header.isChecked())

    def _toggle(self) -> None:
        self._sync()
        if self.panel is not None:
            self.panel.remember(self.key, self.header.isChecked())

    @property
    def expanded(self) -> bool:
        return self.header.isChecked()


class WhyPanel(QWidget):
    """Explanation of how the selected entry was identified, source by source."""

    # Emitted with a tier name when a card's Run button is pressed.
    run_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entry: Optional[BookEntry] = None
        self._extra_selected = 0
        self._open: Dict[str, bool] = {key: True for key in DEFAULT_OPEN}
        self._stats: Dict[str, int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # No heading label: the cards start at the very top of the window, and the
        # selected book's name is the title of its own Summary card.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.scroll, stretch=1)

        self.container = QWidget()
        self.cards = QVBoxLayout(self.container)
        self.cards.setContentsMargins(0, 0, 6, 0)
        self.cards.setSpacing(7)
        self.cards.addStretch(1)
        self.scroll.setWidget(self.container)

        self._rebuild()

    # ------------------------------------------------------------------ state

    def remember(self, key: str, expanded: bool) -> None:
        """Keep a card's fold state so it survives moving between rows."""
        self._open[key] = expanded

    def set_stats(self, entries: Iterable[BookEntry]) -> None:
        """Refresh the library card. Called after a scan, and after any status change."""
        counts: Dict[str, int] = {}
        total = 0
        for entry in entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
            total += 1
        counts['total'] = total
        self._stats = counts
        self._rebuild()

    def show_entry(self, entry: Optional[BookEntry],
                   extra_selected: int = 0) -> None:
        """Explain ``entry``. ``extra_selected`` is how many more rows are selected -
        shown so a bulk action is never mistaken for a single-row one."""
        self.entry = entry
        self._extra_selected = max(0, extra_selected)
        self._rebuild()

    # -------------------------------------------------------------- rendering

    def _rebuild(self) -> None:
        while self.cards.count():
            item = self.cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for card in self._build_cards():
            self.cards.addWidget(card)
        self.cards.addStretch(1)

    def _add(self, key: str, title: str, body, badge: str = '',
             colour: str = TEXT, run: str = '',
             has_run: bool = False) -> CollapsibleCard:
        return CollapsibleCard(key, title, body, badge=badge, colour=colour,
                               expanded=self._open.get(key, key in DEFAULT_OPEN),
                               panel=self, parent=self.container, run=run,
                               has_run=has_run)

    def _build_cards(self) -> List[CollapsibleCard]:
        # No library card: the counts live in the toolbar, where they are visible
        # whether or not a row is selected. This panel is about the selected row.
        cards: List[CollapsibleCard] = []

        entry = self.entry
        if entry is None:
            return [self._add('empty', 'Nothing selected',
                              f'<div style="color:{TEXT_FAINT}">Select a row to see '
                              f'every source that was asked about it, what each one '
                              f'said, and which answer was used.</div>')]

        # No summary card: the confidence, the status and the file list are all
        # already on the row you clicked. Repeating them here is furniture.
        # The Fields card is the score at the top of the panel: green when the book is
        # fully identified, amber while something is still missing.
        missing = self._still_missing(entry)
        badge = ('complete' if not missing
                 else 'missing ' + ', '.join(missing))
        if self._extra_selected:
            badge += f'   ·   +{self._extra_selected} more selected'
        cards.append(self._add(
            'fields', 'Fields', self._fields_html(entry), badge=badge,
            colour=STATE_COLOURS[GOOD if not missing else PARTIAL]))
        cards.extend(self._tier_cards(entry))
        cards.append(self._add('files', 'Files', self._files_html(entry),
                               badge=f'{len(entry.audio_files)} audio', colour=TEXT))
        if entry.raw_tags:
            cards.append(self._add('tags', 'Raw tags in the file',
                                   self._tags_html(entry)))
        return cards

    def _tier_cards(self, entry: BookEntry) -> List[CollapsibleCard]:
        """One card per source.

        Every source gets a card whether or not it ran, so "the web search found
        nothing" and "the web search was never asked" are different, visible states -
        an empty panel otherwise looks like a source that silently failed.
        """
        grouped: Dict[str, List[Dict]] = {}
        order: List[str] = []
        for step in entry.trace:
            tier = str(step.get('tier', '') or 'step')
            if tier not in grouped:
                grouped[tier] = []
                order.append(tier)
            grouped[tier].append(step)

        # The five configurable sources always appear, in resolver order, followed by
        # anything else that turned up in the trace (folder reasoning, dedupe, edits).
        sequence = [t for t in SOURCE_ORDER]
        sequence += [t for t in order if t not in sequence]

        cards = []
        for tier in sequence:
            steps = grouped.get(tier, [])
            label = TIER_LABELS.get(tier, tier.title())
            runnable = tier in SOURCE_ORDER
            if not steps:
                # Header only, folded shut: the card exists so you can see the source
                # and press Run, not to tell you at length that nothing happened.
                card = self._add(tier, label, '', badge='not run',
                                 colour=STATE_COLOURS[NOT_RUN],
                                 run=label if runnable else '', has_run=False)
                card.header.setChecked(False)
                card._sync()
                cards.append(card)
                continue

            state, badge = self._tier_verdict(entry, tier, steps)
            if tier == 'api':
                body = self._api_widget(steps)
            else:
                body = ''.join(self._step_html(step) for step in steps)
            cards.append(self._add(
                tier, label, body, badge=badge, colour=STATE_COLOURS[state],
                run=label if runnable else '', has_run=True))
        return cards

    # ------------------------------------------------------------- card verdicts

    @staticmethod
    def _applied_by(steps: List[Dict]) -> List[str]:
        applied = set()
        for step in steps:
            data = step.get('data')
            if isinstance(data, dict):
                applied.update(data.get('applied') or [])
        return sorted(applied)

    @staticmethod
    def _offered_by(steps: List[Dict]) -> Dict[str, str]:
        """Every field a tier actually produced, whether or not it was used.

        This is the difference between "the source found nothing" and "the source found
        something we already had a better version of" - two states the old badge
        collapsed into one flatly untrue "found nothing".
        """
        offered: Dict[str, str] = {}
        for step in steps:
            data = step.get('data')
            if not isinstance(data, dict):
                continue
            result = data.get('result')
            if isinstance(result, dict):
                for name in JUDGED_FIELDS:
                    if result.get(name):
                        offered[name] = str(result[name])
        return offered

    def _tier_verdict(self, entry: BookEntry, tier: str,
                      steps: List[Dict]) -> tuple:
        """(state, badge) for one source card - what it found, and how well it did."""
        applied = self._applied_by(steps)
        offered = self._offered_by(steps)
        held = [item for step in steps
                for item in ((step.get('data') or {}).get('held') or [])
                if isinstance(step.get('data'), dict)]

        if tier == 'quality':
            # This card never finds anything to apply - it takes confidence away - so
            # the usual badge would read "found nothing" about a card that exists
            # precisely because it found something.
            count = len(getattr(entry, 'warnings', []) or [])
            if not count:
                return GOOD, 'nothing looks wrong'
            return PARTIAL, f'{count} value{"" if count == 1 else "s"} look wrong'

        if tier == 'api':
            return self._api_verdict(steps, applied, held)

        if tier == 'metadata' and not applied and not offered:
            # "Found nothing" is true but unhelpful here, and the two cases are very
            # different problems: an untagged rip, or tags that say everything except
            # who wrote the book.
            tags = len(getattr(entry, 'raw_tags', {}) or {})
            return EMPTY, (f'{tags} tag(s), none naming the book' if tags
                           else 'the file carries no tags')

        # A badge says what is still wrong, not what went right. These used to read
        # "found series, series index, kept the better value we had" - a sentence about
        # values the Fields card above already lists, with the one thing worth knowing
        # nowhere in it. A card that contributed says what is still short; a card that
        # contributed nothing says only that, in two words.
        missing = self._still_missing(entry)
        if applied:
            return ((PARTIAL, 'missing ' + ', '.join(missing)) if missing
                    else (GOOD, 'complete'))
        if offered:
            return PARTIAL, 'nothing new'
        if held:
            return PARTIAL, f'lost to {len(held)} better-sourced value(s)'
        return EMPTY, 'nothing found'

    def _api_verdict(self, steps: List[Dict], applied: List[str],
                     held: List[str]) -> tuple:
        """The book-database card, judged across every database that was asked."""
        by_source, asked, winner = {}, [], ''
        for step in steps:
            data = step.get('data')
            if not isinstance(data, dict):
                continue
            by_source.update(data.get('by_source') or {})
            asked = data.get('sources') or asked
            winner = data.get('winner') or winner
        answered = [s for s in (asked or by_source) if by_source.get(s)]
        rows = sum(len(v or []) for v in by_source.values())

        counts = f'{len(answered)}/{len(asked or by_source) or 0} answered'
        missing = self._still_missing(self.entry) if self.entry else []
        if applied or held:
            if missing:
                return PARTIAL, f'missing {", ".join(missing)}   ·   {counts}'
            return GOOD, f'{winner or "matched"}   ·   {counts}'
        if rows:
            return PARTIAL, f'{rows} candidate(s), none good enough   ·   {counts}'
        return EMPTY, f'nothing returned   ·   {counts}'

    @staticmethod
    def _still_missing(entry: BookEntry) -> List[str]:
        """Which of the judged fields the book still lacks, series index aside."""
        missing = []
        for name in JUDGED_FIELDS:
            if name == 'series_index' and not entry.value('series'):
                continue    # no series, so no position in one to be missing
            if not entry.value(name):
                missing.append(name.replace('_', ' '))
        return missing

    # ------------------------------------------------------- book-database card

    def _api_widget(self, steps: List[Dict]) -> QWidget:
        """One collapsible box per database, plus the answer that was chosen.

        The old version printed "audnexus: 1 result(s) ... itunes: nothing returned ..."
        as one run-on paragraph inside the card, which is unreadable at five databases
        and hid the fact that the other four had been asked at all. Every database now
        gets its own foldable box with its own rows and its own parsed top result, and
        the one that won says so.
        """
        by_source: Dict[str, List[Dict]] = {}
        asked: List[str] = []
        errors: Dict[str, str] = {}
        refined: Dict[str, str] = {}
        chosen, message, applied, held, threshold = None, '', [], [], 0.8
        for step in steps:
            message = str(step.get('message', '')) or message
            data = step.get('data')
            if not isinstance(data, dict):
                continue
            by_source.update(data.get('by_source') or {})
            asked = data.get('sources') or asked
            errors.update(data.get('errors') or {})
            refined = data.get('refined_with') or refined
            chosen = data.get('result') or data.get('rejected') or chosen
            applied = data.get('applied') or applied
            held = data.get('held') or held
            threshold = data.get('threshold', threshold)

        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(6)

        summary = QLabel(f'<div style="color:{TEXT};">{html.escape(message)}</div>')
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(summary)

        if refined:
            # Worth saying out loud: the answer below was not found with the query we
            # started from, and which title the second pass used decides everything
            # after it.
            note = QLabel(
                f'<div style="color:{TEXT_DIM};">Searched again using '
                f'<b>{html.escape(str(refined.get("title", "")))}</b>'
                + (f' by {html.escape(str(refined.get("author", "")))}'
                   if refined.get('author') else '')
                + ' - the first pass only had the filename to go on.</div>')
            note.setWordWrap(True)
            column.addWidget(note)

        winner = (chosen or {}).get('source', '')
        for source in (asked or sorted(by_source)):
            rows = sorted(by_source.get(source) or [],
                          key=lambda row: -row.get('score', 0))
            column.addWidget(self._source_card(source, rows, threshold,
                                               won=source == winner,
                                               error=errors.get(source, '')))

        if chosen:
            column.addWidget(self._sub_card(
                'The answer we used' if applied else 'Best candidate (not used)',
                self._chosen_html(chosen, applied, held),
                badge=f'{chosen.get("score", 0):.0%} match',
                colour=STATE_COLOURS[GOOD if applied else PARTIAL],
                key='api::chosen', expanded=True))
        return host

    def _source_card(self, source: str, rows: List[Dict], threshold: float,
                     won: bool, error: str = '') -> QWidget:
        """One database's own box: how many rows, what they were, its best parsed.

        Every one of these carries its own Run button. Five databases behind a single Run
        meant that re-asking the one that failed cost four requests to the four that had
        already answered - and when four of them say "nothing returned", the one you want
        to retry is the specific one.
        """
        run_kwargs = {'run': source, 'has_run': bool(rows or error),
                      # A single colon: "the api tier, this source only". See
                      # Resolver._split_tiers.
                      'run_key': f'api:{source}'}
        if not rows:
            # A source that was throttled, refused or out of quota did not return no
            # rows; it never answered. Saying "nothing returned" sent you hunting for a
            # better query when the fix was a key or a wait.
            body = (f'<div style="color:{STATUS_TEXT["rejected"]};">{html.escape(error)}'
                    f'</div>' if error else
                    f'<i style="color:{TEXT_FAINT}">This database answered, and had '
                    f'nothing matching the query.</i>')
            return self._sub_card(source, body,
                                  badge=error.split(':')[0] if error
                                  else 'nothing matched',
                                  colour=STATE_COLOURS[EMPTY],
                                  key=f'api::{source}', expanded=bool(error),
                                  **run_kwargs)

        best = rows[0].get('score', 0)
        listing = ['<table style="border-collapse:collapse; width:100%;">']
        for row in rows[:8]:
            score = row.get('score', 0)
            hue = (STATUS_HUES['approved'] if score >= threshold
                   else STATUS_HUES['risky'])
            series = (f' <span style="color:{TEXT_FAINT}">'
                      f'({html.escape(str(row.get("series")))} '
                      f'#{html.escape(str(row.get("series_index") or "?"))})</span>'
                      if row.get('series') else '')
            listing.append(
                f'<tr>'
                f'<td style="color:{hue}; padding:2px 10px 2px 0; '
                f'vertical-align:top; white-space:nowrap;">{score:.0%}</td>'
                f'<td style="padding:2px 0;">{html.escape(str(row.get("title") or "?"))}'
                f'{series}<br>'
                f'<span style="color:{TEXT_DIM}; font-size:11px;">'
                f'{html.escape(str(row.get("author") or "unknown author"))}</span>'
                f'</td></tr>')
        listing.append('</table>')
        if len(rows) > 8:
            listing.append(f'<div style="color:{TEXT_FAINT}; padding-top:4px;">'
                           f'...and {len(rows) - 8} more</div>')
        listing.append(self._block('Its best row, parsed', json.dumps(
            {k: v for k, v in rows[0].items() if k not in ('raw',)},
            ensure_ascii=False, indent=1)))

        badge = f'{len(rows)} result(s) · best {best:.0%}'
        if won:
            badge += ' · used'
        return self._sub_card(source, ''.join(listing), badge=badge,
                              colour=STATE_COLOURS[GOOD if best >= threshold
                                                   else PARTIAL],
                              key=f'api::{source}', expanded=won or best >= threshold,
                              **run_kwargs)

    def _sub_card(self, title: str, body: str, badge: str, colour: str, key: str,
                  expanded: bool, run: str = '', has_run: bool = False,
                  run_key: str = '') -> CollapsibleCard:
        return CollapsibleCard(key, title, body, badge=badge, colour=colour,
                               expanded=self._open.get(key, expanded), panel=self,
                               run=run, has_run=has_run, run_key=run_key)

    def _chosen_html(self, chosen: Dict, applied: List[str],
                     held: List[str]) -> str:
        parts = [self._block('Parsed result', json.dumps(
            {k: v for k, v in chosen.items() if k != 'raw'},
            ensure_ascii=False, indent=1))]
        if applied:
            parts.insert(0, f'<div style="color:{STATUS_HUES["approved"]};">'
                            f'Written to: <b>'
                            f'{html.escape(", ".join(applied))}</b></div>')
        for item in held:
            parts.insert(0, f'<div style="color:{STATUS_HUES["risky"]};">'
                            f'{html.escape(str(item))}</div>')
        return ''.join(parts)

    def _step_html(self, step: Dict) -> str:
        message = html.escape(str(step.get('message', '')))
        data = step.get('data') if isinstance(step.get('data'), dict) else {}
        parts = [f'<div style="margin-bottom:4px;">{message}</div>']

        applied = data.get('applied')
        if applied:
            parts.append(
                f'<div style="color:{TEXT}; margin-bottom:4px;">'
                f'used for: <b>{html.escape(", ".join(applied))}</b></div>')

        if data.get('result'):
            parts.append(self._block('Parsed result',
                                     json.dumps(data['result'], ensure_ascii=False,
                                                indent=1)))

        exchange = data.get('exchange')
        if isinstance(exchange, dict):
            parts.append(self._exchange_html(exchange))
        return ''.join(parts)

    def _exchange_html(self, exchange: Dict) -> str:
        """The full LLM call: who was asked, what was sent, what came back."""
        parts = [f'<div style="color:{TEXT_DIM}; margin:6px 0 4px 0;">'
                 f'{html.escape(str(exchange.get("provider", "")))} / '
                 f'<b>{html.escape(str(exchange.get("model", "")))}</b> at temperature '
                 f'{html.escape(str(exchange.get("temperature", "")))}</div>']
        if exchange.get('error'):
            parts.append(f'<div style="color:{STATUS_TEXT["rejected"]};">'
                         f'{html.escape(str(exchange["error"]))}</div>')
        if exchange.get('system'):
            parts.append(self._block('System prompt', str(exchange['system'])))
        if exchange.get('prompt'):
            parts.append(self._block('Question asked', str(exchange['prompt'])))
        if exchange.get('response'):
            parts.append(self._block('Raw reply', str(exchange['response'])))
        return ''.join(parts)

    @staticmethod
    def _block(heading: str, text: str, limit: int = 4000) -> str:
        clipped = text[:limit] + ('\n...(truncated)' if len(text) > limit else '')
        return (f'<div style="color:{TEXT_DIM}; margin-top:6px;">{html.escape(heading)}</div>'
                f'<div style="background:{BG_RAISED}; border:1px solid {BORDER};'
                f' border-radius:4px; padding:6px; margin-top:2px;'
                f' font-family:Consolas,monospace; font-size:11px;'
                f' white-space:pre-wrap; color:{TEXT};">{html.escape(clipped)}</div>')

    # ------------------------------------------------------------ card bodies

    def _library_html(self) -> str:
        if not self._stats:
            return f'<i style="color:{TEXT_FAINT}">Nothing scanned yet.</i>'
        rows = []
        for status in ('pending', 'risky', 'approved', 'rejected', 'duplicate',
                       'applied'):
            count = self._stats.get(status, 0)
            if not count:
                continue
            rows.append(
                f'<tr><td style="color:{STATUS_TEXT.get(status, TEXT_DIM)};'
                f' padding-right:14px;">{status}</td>'
                f'<td style="text-align:right;">{count}</td></tr>')
        rows.append(f'<tr><td style="color:{TEXT_DIM}; padding-top:4px;">total</td>'
                    f'<td style="text-align:right; padding-top:4px;">'
                    f'{self._stats.get("total", 0)}</td></tr>')
        return f'<table style="border-collapse:collapse;">{"".join(rows)}</table>'

    def _summary_html(self, entry: BookEntry) -> str:
        value = entry.confidence()
        colour = confidence_color(value)
        bar = (f'<table cellspacing="0" cellpadding="0" style="width:200px;">'
               f'<tr><td style="background:{colour}; width:{int(value * 200)}px;'
               f' height:4px; font-size:1px;">&nbsp;</td>'
               f'<td style="background:{BORDER}; height:4px; font-size:1px;">'
               f'&nbsp;</td></tr></table>')
        note = ''
        if entry.duplicate_of:
            note = (f'<div style="color:{STATUS_TEXT["duplicate"]};">Flagged as a '
                    f'duplicate of <code>{html.escape(entry.duplicate_of)}</code></div>')
        return (f'<div style="color:{colour}; font-size:15px; font-weight:600;">'
                f'{value:.0%}</div>{bar}'
                f'<div style="color:{STATUS_TEXT.get(entry.status, TEXT_DIM)};'
                f' margin-top:4px;">Status: {html.escape(entry.status)}</div>'
                f'<div style="color:{TEXT_DIM};">{html.escape(entry.entry_id)}</div>'
                f'{note}')

    def _fields_html(self, entry: BookEntry) -> str:
        """Value first and prominent; where it came from sits under it, quietly."""
        rows = []
        for name in IDENTITY_FIELDS:
            field = entry.get_field(name)
            value = (html.escape(str(field.value)) if field.value
                     else f'<i style="color:{TEXT_FAINT}">not found</i>')
            colour = source_color(field.source)
            meta = (f'<span style="color:{colour};">'
                    f'{html.escape(field.source or "no source")}</span>'
                    f'<span style="color:{TEXT_FAINT};"> · '
                    f'{field.confidence:.0%}</span>')
            if field.corroborated_by:
                meta += (f'<span style="color:{TEXT_FAINT};"> · confirmed by '
                         + html.escape(', '.join(field.corroborated_by)) + '</span>')
            rows.append(
                f'<tr>'
                f'<td style="color:{TEXT_FAINT}; padding:3px 12px 3px 0;'
                f' vertical-align:top; white-space:nowrap;">'
                f'{name.replace("_", " ")}</td>'
                f'<td style="padding:3px 0;">'
                f'<div style="color:{TEXT}; font-size:14px;">{value}</div>'
                f'<div style="font-size:10px;">{meta}</div>'
                f'</td></tr>')
        return f'<table style="border-collapse:collapse; width:100%;">{"".join(rows)}</table>'

    def _files_html(self, entry: BookEntry) -> str:
        rows = [f'<div style="color:{TEXT_DIM};">'
                f'{html.escape(display_path(entry.folder))}</div>']
        for name in entry.audio_files:
            rows.append(f'<div style="margin-left:10px;">{html.escape(name)}</div>')
        for name in entry.image_files:
            rows.append(f'<div style="margin-left:10px; color:{TEXT_DIM};">'
                        f'{html.escape(name)}</div>')
        if entry.is_multi_book_folder:
            rows.append(f'<div style="color:{TEXT_FAINT}; margin-top:4px;">'
                        f'This folder holds several separate books; only the file above '
                        f'belongs to this entry.</div>')
        if entry.applied_path:
            rows.append(f'<div style="color:{ACCENT}; margin-top:4px;">Applied to: '
                        f'{html.escape(display_path(entry.applied_path))}</div>')
        return ''.join(rows)

    def _tags_html(self, entry: BookEntry) -> str:
        rows = []
        for key, value in sorted(entry.raw_tags.items()):
            rows.append(
                f'<tr>'
                f'<td style="color:{TEXT_DIM}; padding-right:10px; vertical-align:top;">'
                f'{html.escape(str(key))}</td>'
                f'<td>{html.escape(str(value)[:200])}</td></tr>')
        return f'<table style="border-collapse:collapse;">{"".join(rows)}</table>'
