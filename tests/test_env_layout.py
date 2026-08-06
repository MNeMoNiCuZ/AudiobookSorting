"""One layout, for the one file it renders: the .env a fresh install writes itself.

There is no committed .env.example. The app generates its own .env from the built-in
defaults on first run, so a checked-in copy of that same output was a second file to
keep in step and nothing else - and these tests are what used to demand it existed.
They now hold the generated file to the same standards: every setting present, every
value the built-in default, no credential and no private address anywhere in it.
"""

from __future__ import annotations

import re

import pytest

from scripts import env_layout
from scripts.env_layout import (SECTIONS, is_credential, layout_keys, relayout_env,
                                render)
from scripts.settings import SCHEMA, Settings, _unquote


@pytest.fixture
def generated(tmp_path):
    """The .env a fresh install writes for itself, as text."""
    target = tmp_path / '.env'
    Settings(env_path=target).ensure_file()
    return target.read_text(encoding='utf-8')


# Written by the app as the window is used; not seeded by hand, so not in a fresh file.
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


# ------------------------------------------------------- the file a fresh install gets

def test_a_fresh_env_carries_no_credentials(generated):
    """Nothing that authenticates is ever written by the generator."""
    leaked = {k: v for k, v in _assignments(generated)
              if is_credential(k) and _unquote(v)}
    assert not leaked, f'a generated .env contains credentials: {sorted(leaked)}'


def test_a_fresh_env_leaks_no_private_addresses(generated):
    """A LAN address baked into a default would ship one person's network to everyone."""
    assert not re.search(r'(?:192\.168|10)\.\d+\.\d+', generated)


def test_a_fresh_env_documents_every_setting(generated):
    documented = {k for k, _ in _assignments(generated)}
    assert not set(SCHEMA) - documented - MACHINE_WRITTEN


def test_a_fresh_env_holds_the_built_in_defaults(generated):
    wrong = {k: (_unquote(v), SCHEMA[k][0]) for k, v in _assignments(generated)
             if k in SCHEMA and not is_credential(k) and _unquote(v) != SCHEMA[k][0]}
    assert not wrong, f'generated .env disagrees with the built-in default: {wrong}'


def test_a_fresh_env_loads_back(tmp_path, generated):
    target = tmp_path / 'copy.env'
    target.write_text(generated, encoding='utf-8')
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
