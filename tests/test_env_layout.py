"""One layout, rendered for both .env and .env.example.

The point of scripts.env_layout is that there is no second structure to drift: a
fresh install's .env and the committed example come out of the same table. These
tests hold that line, and hold the committed example to being key-free.
"""

from __future__ import annotations

import re

import pytest

from scripts import env_layout
from scripts.env_layout import (SECTIONS, is_credential, layout_keys, relayout_env,
                                render, render_example)
from scripts.paths import PROJECT_ROOT
from scripts.settings import SCHEMA, Settings, _unquote

EXAMPLE = PROJECT_ROOT / '.env.example'

# Written by the app as the window is used; not seeded by hand, so not in the example.
MACHINE_WRITTEN = {'AO_UI_WINDOW', 'AO_UI_COLUMN_WIDTHS', 'AO_UI_HIDDEN_COLUMNS',
                   'AO_UI_COPY_RECENT_LIST', 'AO_UI_GRID_WINDOW', 'AO_UI_GRID_COLUMNS'}


def _assignments(text: str) -> list:
    return [re.match(r'^([A-Z0-9_]+)=(.*)$', line).groups()
            for line in text.splitlines() if re.match(r'^[A-Z0-9_]+=', line)]


# --------------------------------------------------------------- the layout table

def test_every_setting_has_a_place_in_the_layout():
    """A new SCHEMA key must be filed deliberately, not land in "Other" by default."""
    missing = set(SCHEMA) - set(layout_keys())
    assert not missing, f'add these to scripts/env_layout.SECTIONS: {sorted(missing)}'


def test_no_key_is_placed_twice():
    keys = layout_keys()
    assert len(keys) == len(set(keys))


def test_credentials_come_first():
    """They are the only part a user must supply, so they are not buried."""
    assert SECTIONS[0][0] == 'Credentials'
    keys = layout_keys()
    last_credential = max(i for i, k in enumerate(keys) if is_credential(k))
    non_credentials = [i for i, k in enumerate(keys) if not is_credential(k)]
    assert last_credential < min(non_credentials)


def test_max_tokens_is_not_mistaken_for_a_secret():
    """It ends in a word that looks like one, and it has a real default to ship."""
    assert not is_credential('AO_MAX_TOKENS')
    assert is_credential('AO_SEARCH_BRAVE_KEY')
    assert is_credential('AO_PROVIDER_OPENAI_API_KEY')


# ------------------------------------------------------------ the committed example

def test_example_is_in_sync_with_the_layout():
    """The file is generated. If this fails, run `python -m scripts.env_layout`."""
    assert EXAMPLE.read_text(encoding='utf-8') == render_example(), (
        '.env.example is stale - regenerate it with `python -m scripts.env_layout`')


def test_example_carries_no_credentials():
    leaked = {k: v for k, v in _assignments(EXAMPLE.read_text(encoding='utf-8'))
              if is_credential(k) and _unquote(v)}
    assert not leaked, f'.env.example contains real credentials: {sorted(leaked)}'


def test_example_leaks_no_private_addresses():
    text = EXAMPLE.read_text(encoding='utf-8')
    assert not re.search(r'\b(?:192\.168|10)\.\d+\.\d+', text)


def test_example_documents_every_setting():
    documented = {k for k, _ in _assignments(EXAMPLE.read_text(encoding='utf-8'))}
    assert not set(SCHEMA) - documented - MACHINE_WRITTEN


def test_example_values_are_the_built_in_defaults():
    wrong = {k: (_unquote(v), SCHEMA[k][0])
             for k, v in _assignments(EXAMPLE.read_text(encoding='utf-8'))
             if k in SCHEMA and not is_credential(k) and _unquote(v) != SCHEMA[k][0]}
    assert not wrong, f'example disagrees with the built-in default: {wrong}'


def test_example_loads_as_an_env_file(tmp_path):
    target = tmp_path / '.env'
    target.write_text(EXAMPLE.read_text(encoding='utf-8'), encoding='utf-8')
    loaded = Settings(env_path=target)
    assert loaded.get('AO_PROVIDER') == SCHEMA['AO_PROVIDER'][0]
    # Quoted JSON is the value most likely to survive a round trip badly.
    assert loaded.get('AO_PROVIDER_SANCTUM_EXTRA_BODY') == '{"enable_tools": false}'


# --------------------------------------------------------------- a generated .env

def test_a_fresh_env_is_commented_not_a_wall_of_keys(tmp_path):
    """The whole reason the layout moved into code."""
    target = tmp_path / '.env'
    Settings(env_path=target).ensure_file()
    text = target.read_text(encoding='utf-8')
    assert '# Credentials' in text
    assert '# Identification' in text
    assert text.count('# ===') >= 2 * len(SECTIONS) - 4
    # And it must still parse back to the defaults it was built from.
    assert Settings(env_path=target).get('AO_CONFIDENCE_SCORE') == \
        SCHEMA['AO_CONFIDENCE_SCORE'][0]


def test_saving_from_the_gui_keeps_the_generated_comments(tmp_path):
    target = tmp_path / '.env'
    settings = Settings(env_path=target)
    settings.ensure_file()
    before = target.read_text(encoding='utf-8')

    settings = Settings(env_path=target)
    settings.set('AO_THREADS', '9')
    settings.save()
    after = target.read_text(encoding='utf-8')

    assert before.count('#') == after.count('#'), 'a save ate the comments'
    assert 'AO_THREADS=9' in after
    changed = [a for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == ['AO_THREADS=4'], changed


def test_unknown_keys_are_kept_not_tidied_away(tmp_path):
    """Losing somebody's hand-set key to make the file neat is not a fair trade."""
    text = render({'AO_THREADS': '4', 'AO_SOMETHING_CUSTOM': 'keep me'})
    assert 'AO_SOMETHING_CUSTOM=keep me' in text
    assert '# Other' in text


def test_relayout_refuses_when_a_value_would_change(tmp_path, monkeypatch):
    """The guard that makes re-tidying a live .env safe to run."""
    target = tmp_path / '.env'
    target.write_text('AO_THREADS=4\nAO_PROVIDER_OPENAI_API_KEY=secret\n',
                      encoding='utf-8')
    monkeypatch.setattr(env_layout, 'render', lambda values, **kw: 'AO_THREADS=4\n')
    with pytest.raises(RuntimeError, match='refusing to rewrite'):
        relayout_env(target)
    # The original must be untouched after a refusal.
    assert 'secret' in target.read_text(encoding='utf-8')


def test_relayout_preserves_every_value(tmp_path):
    target = tmp_path / '.env'
    Settings(env_path=target).ensure_file()
    before = Settings(env_path=target)._values
    relayout_env(target)
    assert Settings(env_path=target)._values == before
