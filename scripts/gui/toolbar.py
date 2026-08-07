"""The single toolbar: what can be on it, and in what order.

Both the main window (which builds the thing) and the Settings page (which lets you
rearrange it) read this list, so there is exactly one definition of a toolbar item.

The user's layout is stored as one ``AO_TOOLBAR`` string - the ids in display order,
with ``|`` for a separator. Anything not in that string is simply hidden, which makes
"reorder" and "show/hide" the same edit.
"""

from __future__ import annotations

from typing import List, NamedTuple

SEPARATOR = '|'


class ToolItem(NamedTuple):
    key: str
    glyph: str
    label: str        # shown in the Settings list, and as the accessible name
    tooltip: str


# id -> appearance, in the order the work actually happens: find the books, identify
# them, decide on them, write them out. The default layout below follows that order,
# and the separators mark where one stage ends and the next begins.
TOOL_ITEMS: List[ToolItem] = [
    # Key stays 'scan' so saved AO_TOOLBAR layouts keep working - only the name the
    # user reads changed, because "Scan" said nothing about what it did to the list
    # you already had.
    ToolItem('scan', '⟳', 'Load Input',
             'Read the input folder into the list  (Ctrl+R).\n'
             'With books already loaded you are asked what to keep first.\n'
             'Right-click for the same thing on the selected rows only, and to\n'
             'choose a different input folder.'),
    ToolItem('sources', '☰', 'Sources',
             'Choose which sources identification is allowed to use'),
    ToolItem('identify', '▶', 'Identify',
             'Run the ticked sources over the selected rows  (F4)'),
    ToolItem('warnings', '⚠', 'Warnings',
             'Review suspicious values, fix them, or make a decision in batches'),
    ToolItem('approve', '✔', 'Approve',
             'LMB: Approve selected\n'
             'MMB: Approve over the saved threshold\n'
             'RMB: Choose an approve threshold'),
    ToolItem('reject', '✘', 'Reject',
             'LMB: Reject selected\n'
             'MMB: Decline under the saved threshold\n'
             'RMB: Choose a decline threshold'),
    ToolItem('reset', '⟲', 'Reset',
             'Clear the decision on the selected rows  (F8)'),
    # Monochrome glyphs only - the emoji codepoints for eye/folder/disk render in
    # full colour on Windows and would ignore the theme entirely.
    ToolItem('preview', '▤', 'Preview',
             'Show the folder tree applying would produce, without touching files  (F7)'),
    ToolItem('apply', '➔', 'Finalize',
             'The program auto-saves identification results, edits, and review choices.\n'
             'Finalize is the only step that touches your files. Every approved row - including\n'
             'any edits you typed - is renamed and moved/copied into the output\n'
             'folder. Preview it first; you are asked to confirm before anything moves.'),
    ToolItem('goodreads', '⌕', 'Goodreads',
             'Open a Goodreads search for the selected book in your browser,\n'
             'using whatever author/title we currently have.'),
    ToolItem('undo', '↶', 'Undo',
             'Undo the last thing you did  (Ctrl+Z).\n'
             'Right-click for the full history - edits and applies - and pick how\n'
             'far back to go.'),
    ToolItem('settings', '⚙', 'Settings',
             'Open the settings page  (F12)'),
]

ITEMS_BY_KEY = {item.key: item for item in TOOL_ITEMS}

DEFAULT_LAYOUT = ('scan,sources,identify,warnings,|,approve,reject,reset,|,'
                  'goodreads,preview,apply,undo,|,settings')


# Layouts saved before the Save button was removed and Goodreads added. A stored
# layout is the user's arrangement and is never second-guessed - except when it is
# verbatim an old default, which means it was never arranged at all.
_SUPERSEDED_LAYOUTS = {
    'scan,sources,identify,|,approve,reject,reset,|,preview,apply,undo,|,save,settings',
    'scan,sources,identify,|,approve,reject,reset,|,goodreads,preview,apply,undo,|,settings',
}


def parse_layout(text: str) -> List[str]:
    """A stored layout string -> a list of ids/separators, ignoring anything unknown."""
    if (text or '').strip() in _SUPERSEDED_LAYOUTS:
        text = DEFAULT_LAYOUT
    keys = []
    for raw in (text or '').split(','):
        key = raw.strip()
        if key == SEPARATOR:
            # No leading or doubled separators - they read as a rendering bug.
            if keys and keys[-1] != SEPARATOR:
                keys.append(key)
        elif key in ITEMS_BY_KEY and key not in keys:
            keys.append(key)
    while keys and keys[-1] == SEPARATOR:
        keys.pop()
    return keys or parse_layout(DEFAULT_LAYOUT)


def format_layout(keys: List[str]) -> str:
    return ','.join(keys)
