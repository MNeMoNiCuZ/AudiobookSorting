"""Tier 5: ask a language model to fill whatever the earlier tiers couldn't.

Two modes:

- :meth:`query_book` - one book in isolation.
- :meth:`query_folder` - every book in a folder in a single call. This is both cheaper
  and *more accurate*, because seeing "Book 1..Book 4" together is what reveals the
  shared author and the series name in the first place.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .api_engine import APIEngine, APIError

_SYSTEM_PROMPT = """You are a librarian with deep knowledge of books, series and authors.

You identify audiobooks from messy filenames and partial metadata. Rules:
- Use your own knowledge of real books to complete and correct the information.
- NEVER replace a pseudonym with a real name. Keep author names as the author publishes them.
- If a field is genuinely unknown, return an empty string. Never invent a series that
  does not exist, and never guess a series index you are not confident about.
- A standalone book has no series: return "" for series and series_index.
- The title is the title of that one book alone. It never repeats the series name and
  never carries the book number: for "Mistborn: The Well of Ascension (Book 2)" the
  title is "The Well of Ascension", the series is "Mistborn", series_index is "2".
- The series is the name of the series itself, without the word "Series", "Saga",
  "Trilogy", "Cycle", "Books", "Collection" or "Novels" bolted on, and without a
  leading "The" that is not part of the published name: "The Expanse Series" is
  "The Expanse", "Mistborn Trilogy" is "Mistborn".
- Leave edition and format notes out of every field: "Unabridged", "Audiobook",
  "Audible Edition", "Complete", "Boxed Set", "Remastered" and the like are not part
  of a title, a series or an author's name.
- Return titles and names in their normal published capitalisation.
- Respond with JSON only. No prose, no markdown fences."""

_BOOK_SCHEMA = """Respond with exactly this JSON shape:
{"title": "", "author": "", "series": "", "series_index": "", "confidence": 0.0, "reasoning": ""}

confidence is your own 0-1 estimate that this identification is correct.
reasoning is one short sentence explaining how you identified it."""

_FOLDER_SCHEMA = """Respond with exactly this JSON shape:
{"series": "", "author": "", "books": [
   {"file": "<the exact filename given>", "title": "", "series_index": "",
    "confidence": 0.0}
 ], "reasoning": ""}

Set the top-level "series" and "author" only if ALL the listed files share them.
Every file in the input must appear exactly once in "books"."""


class LLMQueryClient:
    def __init__(self, provider: Optional[str] = None, settings=None):
        self.logger = logging.getLogger(__name__)
        self.api_engine = APIEngine(provider=provider, settings=settings)
        self.provider = self.api_engine.provider
        self.model = self.provider.model
        self.temperature = self.provider.temperature
        self.max_tokens = self.provider.max_tokens
        # The last request/response pair, so the UI can show exactly what was asked
        # and exactly what came back rather than only the parsed result.
        self.last_exchange: Dict[str, Any] = {}

    # -------------------------------------------------------------- one book

    def query_book(self, hints: Dict[str, str],
                   context_files: Optional[List[str]] = None,
                   evidence: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Identify a single book. Returns the parsed JSON dict, or None.

        `evidence` carries what the database and web tiers found but could not use -
        rejected candidates and raw search snippets. They are frequently correct and
        merely scored below a threshold, so the model gets to read them.
        """
        prompt = ['Identify this audiobook.\n']
        if evidence:
            prompt.append(_format_evidence(evidence))
        if context_files:
            prompt.append('Files in the same folder (context - they may be other books '
                          'in the same series, or chapters of this one):')
            prompt.extend(f'- {name}' for name in context_files[:40])
            prompt.append('')
        prompt.append('What we know so far (empty means unknown):')
        for key in ('title', 'author', 'series', 'series_index'):
            prompt.append(f'{key}: {hints.get(key, "") or "(unknown)"}')
        if hints.get('path'):
            # Relative to the scan root. The absolute path leaks the user's drive
            # layout and tells the model nothing - "D:\AI\Projects\..." is not signal.
            prompt.append(f'\nPath (relative to the library root): {hints["path"]}')
        prompt.append('\n' + _BOOK_SCHEMA)

        result = self._call('\n'.join(prompt))
        if not result:
            return None
        return self._normalise_book(result)

    # ------------------------------------------------------------ one folder

    def query_folder(self, folder_name: str, books: List[Dict[str, str]],
                     evidence: Optional[Dict[str, Any]] = None
                     ) -> Optional[Dict[str, Any]]:
        """Identify every book in a folder at once (#11).

        `books` is a list of ``{"file": name, "title": ..., "author": ...}`` dicts.
        Returns ``{"series", "author", "books": [...], "reasoning"}`` or None.
        """
        if not books:
            return None

        prompt = [f'These audio files all live in the folder "{folder_name}".',
                  'They are either several books in one series, or one book split into '
                  'parts. Identify each one.\n',
                  'Files and what we already know about them:']
        for book in books:
            known = ', '.join(f'{k}={v}' for k, v in book.items()
                              if k != 'file' and v) or 'nothing known'
            prompt.append(f'- {book["file"]}  [{known}]')
        if evidence:
            prompt.append('')
            prompt.append(_format_evidence(evidence))
        prompt.append('\n' + _FOLDER_SCHEMA)

        result = self._call('\n'.join(prompt))
        if not result or not isinstance(result.get('books'), list):
            return None

        normalised = []
        for item in result['books']:
            if not isinstance(item, dict):
                continue
            entry = self._normalise_book(item)
            entry['file'] = str(item.get('file', ''))
            normalised.append(entry)
        result['books'] = normalised
        result['series'] = str(result.get('series', '') or '')
        result['author'] = str(result.get('author', '') or '')
        return result

    # --------------------------------------------------------------- helpers

    def _call(self, user_prompt: str) -> Optional[Dict[str, Any]]:
        payload = {
            'messages': [
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'response_format': {'type': 'json_object'},
        }
        self.last_exchange = {
            'provider': self.provider.name,
            'model': self.model or '(server default)',
            'temperature': self.temperature,
            'system': _SYSTEM_PROMPT,
            'prompt': user_prompt,
            'response': '',
            'error': '',
        }

        try:
            raw = self.api_engine.call_api(payload, model=self.model)
        except (APIError, ValueError) as exc:
            self.logger.error('LLM query failed: %s', exc)
            self.last_exchange['error'] = str(exc)
            return None

        self.last_exchange['response'] = raw
        parsed = extract_json(raw)
        if parsed is None:
            self.logger.error('LLM returned unparseable JSON: %.300s', raw)
            self.last_exchange['error'] = 'Response was not parseable JSON'
        return parsed

    @staticmethod
    def _normalise_book(data: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            'title': str(data.get('title', '') or '').strip(),
            'author': str(data.get('author', '') or '').strip(),
            'series': str(data.get('series', '') or '').strip(),
            'series_index': str(data.get('series_index', '') or '').strip(),
            'reasoning': str(data.get('reasoning', '') or '').strip(),
        }
        try:
            confidence = float(data.get('confidence', 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        # A model's self-reported confidence is optimistic; cap it so it can never
        # outrank a real metadata tag or an Audnexus hit.
        out['confidence'] = max(0.0, min(confidence, 0.85))
        return out


def _format_evidence(evidence: Dict[str, Any]) -> str:
    """Render rejected database candidates and raw search snippets for the prompt."""
    lines = ['Evidence gathered by earlier tiers. None of it scored high enough to be '
             'applied automatically, and some of it is about other books entirely - '
             'weigh it, do not copy it blindly.']

    for candidate in (evidence.get('api') or [])[:10]:
        series = (f' [{candidate.get("series")} #{candidate.get("series_index")}]'
                  if candidate.get('series') else '')
        lines.append(f'- {candidate.get("source", "db")}: '
                     f'"{candidate.get("title", "")}" by '
                     f'"{candidate.get("author", "")}"{series}')

    for item in (evidence.get('search') or [])[:8]:
        body = ' '.join(str(item.get('body', '')).split())[:280]
        lines.append(f'- web: {item.get("title", "")}')
        if body:
            lines.append(f'      {body}')

    return '\n'.join(lines) + '\n' if len(lines) > 1 else ''


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of a model response, tolerating fences and prose."""
    if not text:
        return None
    text = text.strip()

    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass

    # Fall back to the outermost {...} span.
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None
