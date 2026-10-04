# Audiobook Organizer

Sorts a messy audiobook collection into a clean `Author/Series NN - Title/` library.

Point it at a folder of unsorted audiobooks. It works out which files belong to which
book, then identifies each one from embedded tags, the file and folder names, five book
databases (Audnexus, Apple Books, Google Books, Open Library, LibriVox), web search, and
optionally a language model. Every value shows where it came from and how confident it
is. You review, correct and approve the results, preview the resulting folder tree, and
the books are copied (or moved) into the output folder under consistent names.

It can also merge chapter files into a single chaptered `.m4b`, write the corrected
metadata back into the audio tags, emit Audiobookshelf and Calibre sidecar files, flag
true duplicates by file hash, and undo any apply.

<img width="2545" height="1382" alt="image" src="https://github.com/user-attachments/assets/f6eae790-05d1-4b00-bb18-604b11b19b2f" />

## Install

Requires Python 3.11.

On Windows, `venv_create.bat` guides you through the setup: it lists your installed
Python versions, creates the virtual environment, upgrades pip and offers to install
`requirements.txt`.

```bash
git clone https://github.com/MNeMoNiCuZ/AudiobookSorting
cd AudiobookSorting
venv_create.bat
```

Or set it up by hand:

```bash
python -m venv venv
venv\Scripts\activate        # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
```

Optional:

- [ffmpeg](https://ffmpeg.org/) on your PATH, for merging chapter files into one `.m4b`.
- A free Google Books API key, entered on the Providers tab in Settings. The other four
  databases need no key.
- An OpenAI-compatible LLM provider (OpenAI, Groq, OpenRouter, Mistral, Anthropic,
  Ollama, LM Studio, or your own endpoint), for the language model tier.

## Usage

```bash
python main.py
```

Or run `launch.bat`, which uses the `venv` if present.

1. Open Settings (**F12**) and set the input and output folders.
2. Load the input folder (**Ctrl+R**).
3. Identify the selected books (**F4**) and correct anything wrong in the table.
4. Approve (**F5**) or reject (**F6**) each book.
5. Preview (**F7**), then apply (**F8**).

Copy mode is the default, so the original files are left in place.

### Hotkeys

| Key | Action |
|-----|--------|
| Ctrl+R | Load the input folder |
| F2 | Edit the current cell, or open the grid editor on a multi-row selection |
| F3 / Ctrl+F | Search |
| F4 | Identify |
| F5 | Approve |
| F6 | Reject |
| F7 | Preview |
| F8 | Apply |
| F9 | Search Goodreads |
| F12 | Settings |
| Ctrl+O | Open the selected books' folders |
| Ctrl+A | Select all |
| Ctrl+Z | Undo |
| Ctrl+Y / Ctrl+Shift+Z | Redo |
| Ctrl+H | Undo history |
| Esc | Cancel the running operation |

### Command line

```bash
python main.py --scan                             # scan and identify, print a report
python main.py --scan --no-identify               # scan only
python main.py --scan --auto-approve 0.9 --apply  # approve at or above 0.9 and apply
python main.py --scan --dry-run                   # print what --apply would do
python main.py --undo-last                        # reverse the last apply
python main.py --undo-all                         # reverse every apply
```

| Flag | Overrides |
|------|-----------|
| `--input DIR` | Input folder |
| `--output DIR` | Output folder |
| `--provider NAME` | LLM provider |
| `--log-level LEVEL` | Log level |

Test LLM provider connections:

```bash
python -m scripts.test_provider --all
```

## Building

```bash
build.bat
```

Produces a standalone `AudiobookOrganizer.exe` in the project root.
