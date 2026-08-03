# Audiobook Organizer

Sort a messy audiobook collection into a clean `Author/Series NN - Title/` library.

It reads embedded tags, parses filenames, looks books up in five audiobook databases,
scrapes the web, and asks a language model — in that order, stopping as soon as it is
confident. Every value it produces carries where it came from and how much it is
trusted, and nothing is written to disk until you approve it.

![AudiobookOrganizer](https://github.com/user-attachments/assets/bce5cd1b-edfa-4f9c-9406-0d2ee5623f88)

---

## Contents

- [Install](#install) · [Run](#run)
- [How a book gets identified](#how-a-book-gets-identified)
- [The review window](#the-review-window)
- [Editing](#editing)
- [Output](#output)
- [Chapter merging](#chapter-merging)
- [Safety and undo](#safety-and-undo)
- [Keyboard](#keyboard)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Development](#development)

---

## Install

```bash
git clone https://github.com/MNeMoNiCuZ/AudiobookSorting
cd AudiobookSorting
venv_create.bat          # or: python -m venv venv
venv/Scripts/activate
pip install -r requirements.txt
```

Optional:

- **[ffmpeg](https://ffmpeg.org/)** on your PATH — only needed to merge chapter files
  into a single `.m4b`.
- **A Google Books API key** — that one database cannot be used without one. Anonymous
  callers share a project whose daily quota is zero, so every request returns HTTP 429.
  A key is free from [console.cloud.google.com](https://console.cloud.google.com)
  (enable the Books API, create an API key) and goes on the Providers tab. The other
  four databases need nothing.
- **An LLM provider** — only for tier 5. The first four tiers resolve a large share of a
  real library without one.

### A standalone .exe

```bash
build.bat
```

Produces `AudiobookOrganizer.exe` in the project root via PyInstaller, icon included.

## Run

```bash
python main.py
```

Point it at your unsorted books on the Settings page (**F12**), press **Ctrl+R** to scan,
then review, approve and save.

Both folders start **empty on purpose.** A default of `input/` would quietly aim a fresh
install at a folder inside the program directory: the first scan appears to work, finds
nothing, and nobody is ever asked. Empty means unset, and the window says so.

**Copy mode is the default**, so the originals are never touched until you deliberately
switch to move mode. There is no dry-run *mode*: **Preview** (F7) is a button that writes
nothing, and Save always writes. A setting that made Save sometimes write and sometimes
not, depending on a checkbox three tabs deep, is worse than either.

---

## How a book gets identified

Five tiers run in order. Each fills in only what the earlier ones could not, and each
records what it contributed, so any result can be traced back.

| # | Tier | What it uses | Cost |
|---|------|--------------|------|
| 1 | **Embedded tags** | `.m4b` / `.mp3` / `.flac`… metadata written by the publisher. DRM'd Audible files included — the tag atoms are readable even when the audio is not | free |
| 2 | **Filename parsing** | `Author - Series 03 - Title`, `[Series 03] Title`, `01. Title`… read from the folder path as well as the filename, because the signal is usually split across both | free |
| 3 | **Book databases** | Audnexus (Audible), Apple Books, Google Books, Open Library, LibriVox | free, networked |
| 4 | **Web search** | Goodreads / Audible / StoryGraph JSON-LD scraping, plus DuckDuckGo snippet mining | free, networked |
| 5 | **Language model** | Any OpenAI-compatible provider | tokens |

### The databases

Sources are **merged, not raced**: every source is asked, every candidate is scored by
fuzzy token-set similarity against what is already known, and the fields are filled from
whichever source has them.

| Source | Strength |
|---|---|
| `audnexus` | Audiobook catalogue. Best series data by a wide margin |
| `itunes` | Apple Books. Good coverage of non-Audible titles |
| `googlebooks` | Widest general coverage; series data is patchy. Needs a free key |
| `openlibrary` | Free catalogue, decent series records |
| `librivox` | Public-domain audiobooks only. Narrow but exact |

Similarity scoring is what makes this work: `The Hobbit` against
`The Hobbit: Or There and Back Again` scores high, where an exact-match test throws the
correct answer away.

### What makes it accurate

- **The folder is the unit of work.** The folder that directly contains audio files is
  one book — unless it contains several full-length books, in which case each file is
  its own entry sharing that folder. Telling those two cases apart is most of the job.
- **Folder-level reasoning.** A folder holding four books is solved in one LLM call.
  Seeing `Book 1..Book 4` together is what reveals the shared author and the series name
  in the first place. It is cheaper *and* more accurate than asking four times.
- **Corroboration.** When two independent tiers agree on a value, confidence rises. That
  is what makes auto-approve-by-confidence trustworthy enough to skip most of the review.
- **Tiers stop early.** If tags and filenames already agree at high confidence, no
  network call is made at all. `AO_ALWAYS_SEARCH_TO_TIER` sets a floor if you would
  rather it always corroborated against the databases.
- **Dirty-output detection.** A scrape can return something that is syntactically a
  title and obviously wrong to a human: an unclosed bracket, a half-stripped HTML
  entity, `(Unabridged)` left on the end, an author in SHOUTING CAPS. No tier fails, so
  nothing catches it — and it gets filed under that name. So it is checked once at the
  end, and the confidence of the affected fields is lowered so they surface for review.
  Nothing is ever silently rewritten; guessing at what the scrape *meant* turns a
  visible problem into an invisible one.
- **Duplicate detection.** Books whose normalised author + title match one you already
  have are flagged, with duration as the tie-breaker — the same title at wildly
  different lengths is usually an abridgement, not a duplicate.

### Caching

Lookups are cached in SQLite. A successful lookup is kept **forever** — an author and a
series index do not change. A *failed* lookup only means "not found today", so misses
expire on a short TTL (`AO_CACHE_MISS_TTL`). Scans resume by default, skipping entries
already resolved in a previous run.

---

## The review window

One horizontal split, running the full height: the review table on the left, the
explanation panel on the right.

**The table** shows cover, files, author, series, index, title, confidence and status.
Rows are painted rather than rendered as plain text — a status stripe down the left
edge, a status pill, the file cell as folder-over-filename, and confidence as a number
over a thin bar so a column of them scans as a shape. Column widths, order, hidden
columns and the window layout are all remembered.

**The why panel** is the answer to "why does it think that". One collapsible card per
thing that happened: the raw tags, what each tier contributed, the quality warnings, and
for the language model the *entire* exchange — system prompt, the exact question asked,
the raw reply, and the parsed result. When an identification is wrong, the reply is the
only thing that explains it. A library card at the top summarises the whole entry set.

**Colour is a language with exactly two words**, spoken consistently everywhere:

1. **Status colour** = review status — the row stripe, the pill, the toolbar counts.
2. **Source colour** = where a value came from — the same hue in the table cell, in the
   fields list, and on that source's card in the panel. Learn "violet = the language
   model" once and it holds everywhere.

Nothing else is coloured. Confidence is a number and a grey bar, because a third colour
system would mean the first two stop meaning anything.

**Filters** above the table cover review status, confidence (including a custom
threshold), a search box over author / series / title / folder, and the *shape* of the
files — which is often what you actually want to act on:

`Multi-file` · `Single file` · `Already one .m4b` · `Shares its folder` ·
`Has companion files` · `Written to disk` · `Unsaved changes` · `Flagged as odd`

`Unsaved changes` is the one to know: values you typed yourself that have not been
written out yet. It answers "am I about to save what I think I am" without reading the
whole table.

**The toolbar** is a single icon row along the bottom, next to the keys that trigger it,
ordered the way the work happens — find the books, identify them, decide on them, write
them out. It is fully rearrangeable (Settings → Toolbar, or right-click it): the layout
is one string of ids, so reordering and showing/hiding are the same edit. All icons are
drawn as vectors rather than font glyphs, so they hold their stroke weight at any size
and take the theme colour.

**The queue** shows what is stacked up when several identifications are running: which
book is being worked on right now, how far into it, and how far through the batch — with
any single job removable and the whole thing cancellable. Every network call and every
filesystem batch runs on a background thread, so the window never freezes and **Esc**
always gets you out.

---

## Editing

Any field can be edited directly in the table. A value you typed is marked as source
`user`, and that is the one value in the program known to be correct — so no
identification run is ever allowed to quietly overwrite it. When a run wants to, it
stops and shows you exactly what it proposes to change, per row, and you pick.

For more than one book at a time, **Edit in a grid** (**F2** on a multi-row selection) is
a small spreadsheet: the selected books down the side, the four identity fields across.

| Key | In the grid |
|-----|-------------|
| `Enter` / `Shift+Enter` | Commit, move down / up — same field, next book |
| `Tab` / `Shift+Tab` | Commit, move right / left — next field, same book |
| `Ctrl+Z` | Undo the last edit made in here, one step at a time |

Enter going *down* is the point: a column is one kind of value, so working down it is
working through the same field on successive books, which is what fixing a series
actually consists of. The whole grid session lands in the main window's history as a
single step, because that is what it was.

The right-click menu carries the bulk operations, grouped **identify / edit / review /
files**:

- Identify the selection, or **identify with one named tier** — or one named database,
  which always goes to the network, because a source you asked for by name replaying a
  cached miss is indistinguishable from that source being broken.
- Set author or series across the selection, or fill the first row's value down. Offered
  only on the column you actually clicked.
- Clear selected cells so they can be identified again.
- **Search Goodreads** for the selection in your browser.
- Open the source folder, or the folder it was written to.
- Copy: the files themselves (paste straight into a file manager), file paths, file
  names, folder paths, or the rows as a Markdown table. Recently-used copy actions are
  promoted to the top level of the menu.
- Merge chapters into one `.m4b`.

---

## Output

`AO_OUTPUT_TEMPLATE` decides where books land, `AO_FILE_TEMPLATE` what the files are
called. Empty fields collapse cleanly, so a standalone book never ends up in a folder
called `" 00 - Title"`.

```
{author}/{series} {series_index:02d} - {title}

  Brandon Sanderson/Mistborn 01 - The Final Empire/
  Patrick Rothfuss/The Name of the Wind/
```

Placeholders: `{author}` `{series}` `{series_index}` (or `{index}`) `{title}`
`{file_index}` `{extension}`. Any number takes a format spec — `{series_index:02d}` pads
to two digits, `{file_index:03d}` to three.

Also available:

- **`AO_RENAME_SUPPORT_FILES`** — rename the files that travel with the book (cover art,
  `.epub`, `.pdf`, `.nfo`, `.cue`, `.txt`) to match the audio. Only when a file is the
  only one of its extension in the folder, because two `.jpg`s renamed to one stem would
  collide.
- **`AO_WRITE_TAGS`** — write the corrected metadata back into the audio files. Folder
  names are lost the moment the library is imported into Audiobookshelf, Plex or
  Prologue; those read tags. This is what makes the organisation portable.
- **`AO_WRITE_SIDECAR`** — emit `metadata.json` (Audiobookshelf) and `.opf` (Calibre)
  next to the book, making the output self-describing.
- **`AO_ILLEGAL_CHARS`** — how characters a title may contain but a filename may not are
  handled. The default is `smart`, a look-alike per character, so
  `Who Goes There? Vol 2: Rising` becomes `Who Goes There Vol 2 - Rising` rather than
  `Who Goes There- Vol 2- Rising`. Also `dash`, `underscore`, `space`, `remove`.

Windows is treated as the hostile case throughout: reserved device names, the
260-character path limit, and trailing dots and spaces that silently vanish are all
handled in one place.

---

## Chapter merging

A book split into forty numbered `.mp3`s becomes one chaptered `.m4b`, each source file
kept as a real chapter marker so seeking still works. Requires ffmpeg.

The name is not asked for: it is rendered from the same output template the rest of the
program files books under, from the metadata already gathered. The dialog exists to show
you what every selected book is *about* to be called before anything is encoded, and to
hold the options that vary — where it lands, what happens to the originals, and the
bitrate. Books nothing is known about are named plainly as such rather than getting an
invented name off chapter one's title tag.

The default bitrate is `same`: it reads the bitrate of the chapter files and encodes at
that, so a merge never throws quality away. Fixed rates are available; 64k is the usual
choice for spoken word. Every setting in that dialog also lives on Settings → Merging,
because a setting that exists only inside one modal is one you can only find by
committing to a merge.

---

## Safety and undo

This tool moves files, so:

- **Copy mode is the default.** The originals stay exactly where they are and a second
  copy is written out, so the input library is never touched. Move mode is opt-in.
- **Preview writes nothing.** **F7** shows the complete destination tree the apply would
  produce — output root, author, series, book folder, every file, with renames marked —
  so you check the shape of the resulting library rather than reading a flat list of
  `x -> y` lines. Collapse all, or collapse just the book folders to see the shape.
- **You are asked before anything moves**, in a dialog that carries the four decisions
  that matter (copy/move, renaming, tags) rather than merely reciting them — so a wrong
  one is fixed there, not three tabs away in Settings.
- **Every apply is journalled.** `Ctrl+Z` walks back edits and applies alike;
  `Ctrl+H` opens the full history as a palette — oldest at the top, the current position
  highlighted, the future greyed out and clickable to redo forward to. Undo puts the
  files back where they came from.
- **An entry only ever touches its own files.** A folder holding four books can be
  applied one book at a time without disturbing the other three. This is the single most
  important correctness rule in the program and it is what the test suite guards hardest.
- **Collisions never silently merge.** Configurable: `suffix` (default), `skip`, `merge`
  or `overwrite`.

---

## Keyboard

| Key | Action |
|-----|--------|
| **Ctrl+R** | Scan the input folder |
| **F4** | Identify the selected rows with the ticked sources |
| **F5** | Approve |
| **F6** | Reject |
| **F8** | Reset to pending |
| **F7** | Preview the apply |
| **F2** | Rename the clicked field — or, on a multi-row selection, open the grid editor |
| **F3** / **Ctrl+F** | Jump to the search box |
| **Ctrl+A** | Select all rows |
| **Ctrl+Z** | Undo |
| **Ctrl+Y** / **Ctrl+Shift+Z** | Redo |
| **Ctrl+H** | Undo history |
| **F12** | Settings |
| **Esc** | Cancel the running operation |

Approving advances to the next row, so a library can be reviewed without touching the
mouse.

---

## Configuration

Everything lives in `.env` at the project root. The **Settings page (F12)** reads and
writes that same file, so anything configurable in the UI is a key in `.env` and vice
versa — you never have to edit it by hand, but you can. Start from
[`.env.example`](.env.example), which documents every key with its default.

Values resolve as **process environment > `.env` > built-in default**, and saving
preserves the comments and key order already in the file.

`.env` is gitignored. Your API keys are never committed.

Tabs: **Identification** · **Output** · **Merging** · **Cache** · **Interface** ·
**Toolbar** · **General** · **Providers**. Every control is generated from the schema in
`scripts/settings.py`, so adding a setting there makes it appear in the UI
automatically. There are no spin boxes anywhere in the application — their arrows are a
12-pixel target for a value you already know how to type.

### LLM providers

Any endpoint speaking the OpenAI chat-completions format works. Adding one takes three
keys and no code:

```ini
AO_PROVIDERS=sanctum,myserver
AO_PROVIDER_MYSERVER_BASE_URL=http://localhost:1234/v1
AO_PROVIDER_MYSERVER_API_KEY=sk-...
AO_PROVIDER_MYSERVER_MODEL=some-model
```

Preconfigured out of the box:

| Provider | Base URL | Notes |
|---|---|---|
| `sanctum` | _(you fill this in)_ | Provider-agnostic gateway; the model id routes to whatever backend the admin configured |
| `openai` | `https://api.openai.com/v1` | |
| `groq` | `https://api.groq.com/openai/v1` | Fast, generous free tier |
| `openrouter` | `https://openrouter.ai/api/v1` | Access to most models |
| `mistral` | `https://api.mistral.ai/v1` | |
| `anthropic` | `https://api.anthropic.com/v1` | OpenAI-compatibility endpoint |
| `ollama` | `http://localhost:11434/v1` | Local, no key needed |
| `lmstudio` | `http://localhost:1234/v1` | Local, no key needed |

Per-provider knobs: `AUTH_STYLE` (`bearer` / `x-api-key` / `none`), `EXTRA_BODY` (JSON
merged into every request), `SUPPORTS_JSON_MODE`, `SUPPORTS_SEED`.

The Providers tab has **Test connection** and a **Refresh** that lists the models the
endpoint actually offers, so a model id is picked from a list rather than typed from
memory. From the command line:

```bash
python -m scripts.test_provider --all
```

---

## Command line

Useful for large libraries, scripting, or running over SSH.

```bash
python main.py --scan                             # scan + identify, print a report
python main.py --scan --no-identify               # scan only
python main.py --scan --auto-approve 0.9 --apply  # approve high-confidence, apply them
python main.py --scan --dry-run                   # print what --apply would do
python main.py --undo-last                        # reverse the last apply
python main.py --undo-all
```

Overrides that don't touch `.env`: `--input`, `--output`, `--provider`, `--log-level`.

---

## Development

```bash
pytest tests -q
```

**128 tests**, covering the scanner's layout heuristics, the provenance model, the
resolution chain, path templating, the settings schema, chapter-merge progress, window
behaviour and — most importantly — that applying one book never moves its siblings.
No test touches the network; a fixture fails the run loudly if anything tries.

`tests/demo_book_databases.py` is the exception and is not a test: it queries the real
databases and prints the exact URL, the HTTP status, the timing, every row returned with
its score, and the merge across all of them. It exists so that
"audnexus: 1 result / itunes: nothing returned" in the panel is *checkable* — run it, see
the URLs, paste one into a browser.

```bash
python tests/demo_book_databases.py "Ghost of the Shadowfort" "T.C. Edge"
python tests/demo_book_databases.py --sources audnexus,itunes --raw "The Hobbit"
```

### Layout

```
main.py                    GUI + CLI entry point
scripts/
  settings.py              .env-backed configuration + the schema the UI is built from
  api_engine.py            generic OpenAI-compatible LLM client
  models.py                BookEntry / Field - the typed model carrying provenance
  file_scanner.py          directory tree -> entries
  metadata_extractor.py    tier 1: embedded tags
  regex_parser.py          tier 2: names
  api_query.py             tier 3: Audnexus / Apple / Google / OpenLibrary / LibriVox
  web_search.py            tier 4: scraping + search snippets
  llm_query.py             tier 5: the model
  resolver.py              runs the chain, merges by confidence, records the trace
  quality.py               dirty-output detection
  dedupe.py                duplicate detection
  cache.py                 SQLite lookup cache
  file_operations.py       the apply engine
  journal.py               undo
  paths.py                 templating + Windows path safety
  tag_writer.py            metadata written back into the files
  sidecar.py               metadata.json + .opf
  chapter_merge.py         ffmpeg chapter merging
  workers.py               Qt background threads
  data_manager.py          persistence for reviewed entries
  gui/                     PyQt6 dark-mode interface
```
