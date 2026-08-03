"""Shared helpers - logging setup and small formatting utilities (#7)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False

# Chatty third-party loggers that would otherwise drown out our own output.
_NOISY = ('httpx', 'httpcore', 'urllib3', 'requests', 'charset_normalizer',
          'PIL', 'duckduckgo_search', 'primp')


def setup_logging(level: str = 'INFO', log_file: Optional[Path] = None,
                  force: bool = False) -> logging.Logger:
    """Configure root logging exactly once.

    The old version added a handler on every construction, so after N instantiations
    every message appeared N times. This is idempotent.
    """
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(logging.DEBUG)  # handlers filter; root must let everything through

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric)
    console.setFormatter(logging.Formatter('%(levelname)-7s %(name)-28s %(message)s'))
    root.addHandler(console)

    if log_file is None:
        from .paths import PROJECT_ROOT
        log_file = PROJECT_ROOT / 'audiobook_organizer.log'
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-7s %(name)-28s %(message)s'))
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning('Could not open log file %s: %s', log_file, exc)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def human_size(num_bytes: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num_bytes) < 1024:
            return f'{num_bytes:.0f} {unit}' if unit == 'B' else f'{num_bytes:.1f} {unit}'
        num_bytes /= 1024
    return f'{num_bytes:.1f} PB'


def human_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ''
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m'


def truncate(text: str, length: int = 60) -> str:
    text = str(text or '')
    return text if len(text) <= length else text[:length - 1] + '…'
