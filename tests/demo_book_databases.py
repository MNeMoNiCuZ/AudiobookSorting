"""Prove what the book databases actually do, one query at a time.

Run it from the project root or from inside tests/ - either works:

    python tests/demo_book_databases.py "Ghost of the Shadowfort" "T.C. Edge"
    python tests/demo_book_databases.py --query "sanderson mistborn 01"
    python tests/demo_book_databases.py --sources audnexus,itunes "The Hobbit" "Tolkien"
    python tests/demo_book_databases.py --raw "Ghost of the Shadowfort"

This is the one file in tests/ that is not a test: it talks to the real databases over
the network, so pytest never collects it (no ``test_`` prefix) and it is never run by
the suite.

For every configured database this prints:

  * the exact URL that was requested,
  * how long it took and what HTTP status came back,
  * every row the source returned, scored against the query,
  * (with --raw) the untouched JSON body,

and then the merge across all of them: the winner, why it won, and the parsed result
the resolver would hand to the rest of the program.

The point is that "audnexus: 1 result / itunes: nothing returned" in the panel is
checkable. Run this, see the URLs, paste one into a browser.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

# Run as a script from inside tests/, sys.path[0] is tests/ and "scripts" is invisible.
# conftest.py does the same thing for the suite; a script has to do it for itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.api_query import AVAILABLE_SOURCES, BookAPIClient  # noqa: E402
from scripts.settings import get_settings  # noqa: E402

BAR = '=' * 78

# A source's id is not always the host it talks to: the Audnexus tier searches
# Audible's own catalogue endpoint, so its requests are rate-limited under "audible".
HOST_ALIASES = {'audible': 'audnexus'}


def run(title: str, author: str, query: str, sources: List[str], threshold: float,
        keep_raw: bool) -> int:
    hints = {k: v for k, v in
             (('title', title), ('author', author), ('query', query)) if v}

    print(BAR)
    print('QUERY')
    print(BAR)
    print(json.dumps(hints, indent=2, ensure_ascii=False))
    print(f'sources    : {", ".join(sources)}')
    print(f'threshold  : {threshold:.0%}   (a candidate below this is not used)')
    print()

    client = BookAPIClient(cache=None, sources=sources, threshold=threshold,
                           timeout=30, query_all=True,
                           google_key=get_settings().get('AO_GOOGLE_BOOKS_KEY', ''))
    client.keep_raw = keep_raw
    started = time.time()
    best = client.search(hints, force=True)
    elapsed = time.time() - started

    refined = client.last_refined_with
    if refined:
        print(BAR)
        print('SECOND PASS')
        print(BAR)
        print('  The first pass had no title of its own, so it searched with the '
              'filename.')
        print(f'  The best answer to that was {refined["title"]!r} by '
              f'{refined["author"]!r},')
        print('  and the sources below were asked again using it. Requests marked '
              '"pass 2"')
        print('  are that second round.')
        print()

    for source in sources:
        rows = sorted(client.last_by_source.get(source) or [],
                      key=lambda row: -row.get('score', 0))
        calls = [c for c in client.last_requests
                 if HOST_ALIASES.get(c['source'], c['source']) == source]

        print(BAR)
        print(f'{source.upper()}   -   {len(rows)} row(s)')
        print(BAR)
        if not calls:
            print('  NOT CALLED - this source had nothing it could build a query from')
            print('  (audnexus and itunes need keywords; librivox needs a title or an '
                  'author)')
            print()
            continue

        for call in calls:
            tag = f'pass {call["pass"]}' + (f', try {call["attempt"]}'
                                            if call['attempt'] > 1 else '')
            print(f'  GET      [{tag}] {call.get("url", "?")}')
            detail = []
            if 'status' in call:
                detail.append(f'HTTP {call["status"]}')
            if 'bytes' in call:
                detail.append(f'{call["bytes"]} bytes')
            if 'elapsed' in call:
                detail.append(f'{call["elapsed"]:.2f}s')
            print(f'  RESULT   {"  ·  ".join(detail) or "-"}')
            if call.get('error'):
                print(f'  ERROR    {call["error"]}')
            if call.get('note'):
                print(f'  NOTE     {call["note"]}')

        if not rows:
            # The distinction the old wording erased: a source that was throttled or is
            # out of quota did not answer and had nothing - it never answered at all.
            why = client.last_errors.get(source)
            print(f'  ROWS     none - {why}' if why else
                  '  ROWS     none - the source answered, and had nothing matching')
        for index, row in enumerate(rows, start=1):
            mark = '*' if row.get('score', 0) >= threshold else ' '
            series = (f'   [{row.get("series")} #{row.get("series_index") or "?"}]'
                      if row.get('series') else '')
            print(f'  {mark} {index:>2}. {row.get("score", 0):5.0%}  '
                  f'{row.get("title") or "?"}{series}')
            print(f'          by {row.get("author") or "unknown"}')
        if rows:
            print()
            print('  Top row, parsed:')
            for line in json.dumps(rows[0], indent=2,
                                   ensure_ascii=False).splitlines():
                print(f'    {line}')
        for call in [c for c in calls if keep_raw and "raw" in c]:
            print()
            print('  Raw response body:')
            body = json.dumps(call['raw'], indent=2, ensure_ascii=False)
            for line in body.splitlines()[:400]:
                print(f'    {line}')
            if len(body.splitlines()) > 400:
                print('    ...(truncated)')
        print()

    print(BAR)
    print('MERGED ACROSS EVERY SOURCE')
    print(BAR)
    print(f'  {len(client.last_candidates)} candidate(s) in {elapsed:.2f}s '
          f'from {len(client.last_requests)} request(s)'
          + (' over two passes' if refined else ''))
    if client.last_errors:
        for name, why in client.last_errors.items():
            print(f'  FAILED   {name}: {why}')
    if best:
        print(f'  WINNER   {best.get("source")}  at {best.get("score", 0):.0%}')
        if best.get('filled_from'):
            print(f'  FILLED   ' + ', '.join(
                f'{field} from {where}'
                for field, where in best['filled_from'].items()))
        print()
        print('  What the resolver would write:')
        for line in json.dumps({k: v for k, v in best.items() if k != 'raw'},
                               indent=2, ensure_ascii=False).splitlines():
            print(f'    {line}')
    else:
        rejected = client.last_rejected
        print('  WINNER   none reached the threshold, so nothing would be written')
        if rejected:
            print(f'  CLOSEST  {rejected.get("source")} at '
                  f'{rejected.get("score", 0):.0%}: '
                  f'{rejected.get("title")} - {rejected.get("author")}')
            print(f'           lower the Confidence Score below '
                  f'{rejected.get("score", 0):.0%} to accept it')
    print()
    return 0


def main(argv=None) -> int:
    settings = get_settings()
    known = [key for key, _label, _blurb in AVAILABLE_SOURCES]

    parser = argparse.ArgumentParser(
        prog='demo_book_databases.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('title', nargs='?', default='', help='book title')
    parser.add_argument('author', nargs='?', default='', help='author name')
    parser.add_argument('--query', default='',
                        help='free-text search instead of a title/author pair, '
                             'exactly as the resolver falls back to when the local '
                             'tiers found nothing')
    parser.add_argument('--sources', default='',
                        help=f'comma-separated subset of: {", ".join(known)}. '
                             f'Defaults to whatever AO_API_SOURCES is set to.')
    parser.add_argument('--threshold', type=float, default=None,
                        help='override the confidence score, 0-1')
    parser.add_argument('--raw', action='store_true',
                        help='also print each source\'s untouched JSON body')
    args = parser.parse_args(argv)

    if not (args.title or args.author or args.query):
        parser.error('give a title, an author, or --query')

    sources = [s.strip().lower() for s in (args.sources or '').split(',') if s.strip()]
    sources = sources or settings.get_list('AO_API_SOURCES') or known
    unknown = [s for s in sources if s not in known]
    if unknown:
        parser.error(f'unknown source(s): {", ".join(unknown)}. '
                     f'Known: {", ".join(known)}')

    threshold = (args.threshold if args.threshold is not None
                 else settings.get_float('AO_CONFIDENCE_SCORE', 0.80))
    return run(args.title, args.author, args.query, sources, threshold, args.raw)


if __name__ == '__main__':
    sys.exit(main())
