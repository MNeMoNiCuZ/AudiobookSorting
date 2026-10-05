"""Merge a folder of numbered chapter files into one chaptered .m4b (#33).

Requires ffmpeg on PATH (or `AO_FFMPEG_PATH`). Each source file becomes a chapter
marker, so seeking still works in the merged book.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .paths import temp_dir

logger = logging.getLogger(__name__)

# (done, total, message). `total` is 0 while the size of the job is not yet known.
ProgressCallback = Optional[Callable[[int, int, str], None]]


def ffmpeg_available(ffmpeg: str = 'ffmpeg') -> bool:
    return shutil.which(ffmpeg) is not None


def probe_bitrate(path: Path) -> int:
    """Bits per second of one source file, or 0 when it cannot be read."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        return int(getattr(getattr(audio, 'info', None), 'bitrate', 0) or 0)
    except Exception:
        return 0


def choose_bitrate(files: List[Path], requested: str = 'same') -> str:
    """Turn the bitrate setting into something ffmpeg will take.

    "same" means "do not throw quality away": the highest bitrate found across the
    chapter files, rounded to a sane AAC rate and clamped to 320k. Re-encoding a 128k
    mp3 down to 64k because 64k is "the usual choice for spoken word" loses half the
    file for no reason the user asked for, which is why this is the default.
    """
    requested = (requested or 'same').strip().lower()
    if requested and requested != 'same':
        return requested

    rates = [probe_bitrate(path) for path in files]
    best = max(rates) if rates else 0
    if best <= 0:
        return '128k'                       # nothing readable - a safe middle
    kbps = min(320, max(32, round(best / 1000)))
    # Snap up to the nearest standard rate, so ffmpeg is not handed "117k".
    for standard in (32, 48, 64, 96, 128, 160, 192, 256, 320):
        if kbps <= standard:
            return f'{standard}k'
    return '320k'


def aac_sources(files: List[Path]) -> bool:
    """True when every chapter is already AAC inside an MP4-family container.

    This is the difference between a merge that takes a minute and one that takes a
    second. Re-encoding AAC to AAC at the same bitrate is not "merging" - it is paying
    for the whole encode twice and losing a generation of quality to do it. When the
    sources are already what a .m4b holds, the audio can be copied in bit for bit.

    The sample rate and channel count have to match across the whole book as well, and
    not as a nicety: one stream cannot change format halfway through, so ffmpeg's
    concat demuxer responds to a chapter that does not match by *silently dropping it*.
    Two 60-second chapters at different sample rates copy into a 60-second book with no
    error at all, which is the worst failure this code could have - a file that looks
    finished and is missing half the story.
    """
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return False
    if not files:
        return False

    formats = set()
    for path in files:
        if path.suffix.lower() not in ('.m4a', '.m4b', '.mp4'):
            return False
        try:
            audio = MP4(str(path))
        except Exception:
            return False
        info = getattr(audio, 'info', None)
        # 'mp4a.40.2' is AAC-LC; ALAC reports 'alac' and must still be encoded.
        codec = str(getattr(info, 'codec', '') or '')
        if not codec.startswith('mp4a'):
            return False
        formats.add((codec, getattr(info, 'sample_rate', None),
                     getattr(info, 'channels', None)))
        if len(formats) > 1:
            return False
    return True


def common_format(files: List[Path], ffmpeg: str = 'ffmpeg') -> Tuple[int, int]:
    """One sample rate and channel count for the whole book: the highest of each.

    Every chapter has to be encoded to the *same* format, because the segments are
    joined by copying and one stream cannot change format partway through - a segment
    that does not match is dropped without an error. Taking the maximum rather than the
    first file's format means a book with one mono chapter among stereo ones is not
    silently downmixed to mono throughout.

    Falls back to 44100/2, which is valid for AAC and safe for anything.
    """
    rates, channels = [], []
    for path in files:
        try:
            from mutagen import File as MutagenFile
            info = getattr(MutagenFile(str(path)), 'info', None)
        except Exception:
            info = None
        rate = int(getattr(info, 'sample_rate', 0) or 0)
        count = int(getattr(info, 'channels', 0) or 0)
        if rate:
            rates.append(rate)
        if count:
            channels.append(count)
    # AAC has no 8 kHz-and-below problem, but ffmpeg will not take a rate of 0.
    return (max(rates) if rates else 44100,
            min(2, max(channels)) if channels else 2)


def probe_duration(path: Path, ffmpeg: str = 'ffmpeg') -> float:
    """Length in seconds. Uses mutagen first - far cheaper than spawning ffprobe."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        if audio is not None and getattr(audio, 'info', None):
            length = getattr(audio.info, 'length', 0)
            if length:
                return float(length)
    except Exception:
        pass

    ffprobe = 'ffprobe' if ffmpeg == 'ffmpeg' else str(Path(ffmpeg).with_name('ffprobe'))
    if not shutil.which(ffprobe):
        return 0.0
    try:
        output = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, timeout=60)
        return float(output.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def chapter_title(path: Path, index: int) -> str:
    """A readable chapter name from one filename, falling back to "Chapter N"."""
    stem = re.sub(r'^[\s\d._-]+', '', path.stem).strip(' -_.')
    stem = re.sub(r'\s{2,}', ' ', stem)
    return stem or f'Chapter {index}'


def chapter_titles(files: List[Path]) -> List[str]:
    """Chapter names for a whole book, judged against each other.

    Naming each chapter from its own filename in isolation is what produced twelve
    chapters all called "Rich Man's Sky": these files are named
    ``01 - Wil McCarthy - Rich Man's Sky.mp3``, and everything after the number is the
    *book*, identical in all of them. A chapter name that is the same for every chapter
    is not a chapter name.

    So the whole list is compared: whatever prefix and suffix every stem shares is the
    book, and what is left is the chapter. If nothing distinctive is left - or all that
    remains is the track number - they are numbered instead, which is honest and
    useful, where a repeated title is neither.
    """
    stems = [Path(path).stem for path in files]
    if not stems:
        return []
    if len(stems) == 1:
        return [chapter_title(Path(files[0]), 1)]

    prefix = _common_prefix(stems)
    suffix = _common_suffix([stem[len(prefix):] for stem in stems])

    titles = []
    for index, stem in enumerate(stems, start=1):
        core = stem[len(prefix):len(stem) - len(suffix) if suffix else None]
        # A leading track number is not part of the chapter's name - "01 - The
        # Arrival" is "The Arrival". Only stripped when something follows it, so a
        # chapter actually called "1984" survives.
        core = re.sub(r'^\d{1,4}\s*[-._)]?\s*(?=\S)', '', core)
        core = re.sub(r'\s{2,}', ' ', core).strip(' -_.,')
        # A bare number is the track number we already know; "Chapter 3" says it
        # better than "3" does, and "" says nothing at all.
        if not core or re.fullmatch(r'\d{1,4}(\.\d+)?', core):
            titles.append(f'Chapter {index}')
        else:
            titles.append(core)

    # Every name identical means the split above found nothing to split on.
    if len(set(titles)) == 1 and len(titles) > 1:
        return [f'Chapter {index}' for index in range(1, len(stems) + 1)]
    return titles


def _common_prefix(values: List[str]) -> str:
    """The longest leading run shared by every string, trimmed to a word boundary."""
    import os
    shared = os.path.commonprefix(values)
    # Cutting mid-word turns "Chapter 1"/"Chapter 12" into a prefix of "Chapter 1",
    # which would leave "" and "2". Back off to the last separator.
    match = re.search(r'^.*[\s\-_.]', shared)
    return match.group() if match else ''


def _common_suffix(values: List[str]) -> str:
    reversed_prefix = _common_prefix([value[::-1] for value in values])
    return reversed_prefix[::-1]


def merge_to_m4b(files: List[Path], output_path: Path, ffmpeg: str = 'ffmpeg',
                 bitrate: str = 'same', title: str = '', author: str = '',
                 on_progress: ProgressCallback = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> Tuple[bool, str]:
    """Concatenate `files` into a single chaptered .m4b.

    Returns ``(success, message)``. Never raises for the expected failure modes.

    Progress is counted in chapters, because that is the unit the work is actually
    done in and the unit you can check against the folder in front of you. A book of
    twelve chapters is fourteen steps:

        step 1        preparing - reading the source bitrate and every duration
        steps 2..13   one per chapter, ticked over as that chapter finishes
        step 14       joining the chapters and checking the result

    Nothing between two chapters moves the bar, and nothing moves it twice. The
    version before this one drove the bar off ffmpeg's own timestamps and off phase
    weightings, which put it at 90% within seconds of starting and left it there for
    the rest of the encode - a bar that is wrong in the direction of "nearly done" is
    worse than no bar. What is happening *inside* the current chapter is still said in
    the message, where a number that jitters costs nothing.
    """
    if not files:
        return False, 'No files to merge'
    if not ffmpeg_available(ffmpeg):
        return False, (f'ffmpeg not found (looked for {ffmpeg!r}). Set AO_FFMPEG_PATH '
                       f'on the Settings page or install ffmpeg.')

    files = [Path(f) for f in files]
    missing = [f for f in files if not f.is_file()]
    if missing:
        return False, f'Missing source file: {missing[0]}'

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cancelled = should_cancel or (lambda: False)
    # One step to prepare, one per chapter, one to finish. The denominator is a count
    # of real things, so "4 / 14" can be checked against the folder.
    total_steps = len(files) + 2
    highest = 0.0

    def report(step: float, message: str) -> None:
        """Emit (chapters done, total steps, what is happening).

        The step is a *fraction* of a chapter, not a whole one. Counting only finished
        chapters left the bar motionless for the minutes an encode spends inside one,
        while the message beside it counted smoothly up through the audio - two
        readouts of the same job disagreeing, and the wrong one was the bar. So the
        bar is driven by the same measured audio the percentage is, and the whole
        number it is displayed as is still a chapter count.

        It never goes backwards. Chapters encode concurrently and finish out of order,
        and a bar that ticks 5, 4, 6 reads as a bug rather than as what it is.
        """
        nonlocal highest
        highest = max(highest, max(0.0, min(float(total_steps), float(step))))
        if on_progress:
            on_progress(highest, total_steps, message)

    report(0, f'Preparing {output_path.name}...')
    # Nothing needs encoding at all if the chapters are already AAC and no particular
    # rate was asked for - see aac_sources. This is checked before choose_bitrate
    # because in copy mode the answer would be thrown away anyway.
    copy_only = (bitrate or 'same').strip().lower() == 'same' and aac_sources(files)
    bitrate = 'copy' if copy_only else choose_bitrate(files, bitrate)

    # Segment files land in the project's own temp/, not in %TEMP% - a merge that is
    # killed leaves gigabytes of half-encoded audio behind, and it belongs somewhere
    # visible rather than scattered through the system temp folder.
    workdir = temp_dir('merge')
    try:
        # Chapter names are decided for the list as a whole - see chapter_titles.
        names = chapter_titles(files)

        # Still the preparing step: reading a duration takes milliseconds, and
        # spending a twelfth of the bar on each of them is what made the old one
        # sprint to the end and stop.
        durations_ms: List[int] = []
        for index, path in enumerate(files, start=1):
            if cancelled():
                return False, 'Cancelled before encoding started'
            report(0, f'Reading chapter {index} of {len(files)}: {path.name}')
            duration_ms = int(probe_duration(path, ffmpeg) * 1000)
            if duration_ms <= 0:
                return False, f'Could not read the duration of {path.name}'
            durations_ms.append(duration_ms)
        offset_ms = sum(durations_ms)
        source_durations_ms = list(durations_ms)

        list_file = workdir / 'inputs.txt'
        metadata_file = workdir / 'chapters.txt'

        def join(sources: List[Path], lengths: List[int], codec_args: List[str],
                 start: float, message: str,
                 per_chapter: bool = False) -> Tuple[bool, str, int]:
            """Concatenate `sources` into the output, with chapters at `lengths`.

            Returns ``(ok, detail, total_ms)``. When the audio is being copied rather
            than encoded, the result is *verified* before being called a success - see
            below.

            `per_chapter` drives the bar off which chapter this single ffmpeg run has
            reached. It is on for the copy-only path, where the one run *is* the whole
            job, and off for the final join after a parallel encode, where the
            chapters have already been counted.
            """
            total_ms = sum(lengths)
            # ffmpeg's concat demuxer needs a list file with escaped paths.
            with open(list_file, 'w', encoding='utf-8') as handle:
                for path in sources:
                    escaped = (str(path.resolve()).replace('\\', '/')
                               .replace("'", r"'\''"))
                    handle.write(f"file '{escaped}'\n")
            metadata_file.write_text(
                _metadata_text(title, author, names, lengths), encoding='utf-8')

            report(start, message)
            # `-progress pipe:1` makes ffmpeg print `out_time_ms=...` as it goes. That
            # is the only honest source of encode progress there is, and we know what
            # to divide it by because the durations were just measured.
            command = [
                ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-progress', 'pipe:1', '-nostats',
                '-f', 'concat', '-safe', '0', '-i', str(list_file),
                '-i', str(metadata_file), '-map_metadata', '1', '-map_chapters', '1',
                *codec_args, '-vn',
                '-f', 'mp4', str(output_path),
            ]
            ok, detail = _run_with_progress(
                command, lengths=lengths if per_chapter else [],
                name=output_path.name, report=report, cancelled=cancelled,
                start=start)

            # A copy that "succeeded" is not taken on its word. The concat demuxer
            # drops any chapter whose format differs from the first one and reports no
            # error at all, so two 60-second chapters at different sample rates copy
            # into a 60-second book that looks finished. The format checks elsewhere
            # exist to prevent that, but they read tags - this reads the result.
            if ok and 'copy' in codec_args and total_ms > 0:
                written_ms = probe_duration(output_path, ffmpeg) * 1000
                if written_ms < total_ms - 2000:
                    ok = False
                    detail = (f'joining produced {written_ms / 60000:.0f} min of a '
                              f'{total_ms / 60000:.0f} min book')
            return ok, detail, total_ms

        # Preparing is done; every step from here is a chapter.
        report(1, f'Preparing {output_path.name} - {len(files)} chapters, '
                  f'{offset_ms / 60000:.0f} min')

        ok, detail = False, ''
        if copy_only:
            ok, detail, offset_ms = join(
                files, source_durations_ms, ['-c:a', 'copy'], 1,
                f'Copying {len(files)} chapters into {output_path.name} '
                f'(no re-encoding needed)...', per_chapter=True)
            if not ok and not cancelled():
                # Copying cannot always work, and that is a reason to encode this book
                # rather than to fail it. Encoding falls to the path below, which
                # re-encodes each chapter separately and so normalises them on the way.
                logger.info('Copy-merge of %s failed (%s); encoding instead',
                            output_path.name, detail)
                _discard(output_path)
                copy_only = False
                bitrate = choose_bitrate(files, 'same')
                # A copy that got some way through counted chapters that are about to
                # be encoded again. The bar has to be allowed back down for that.
                highest = 1.0

        workers = 1
        if not ok and not copy_only and not cancelled():
            # Each chapter is encoded on its own and the results are joined by copying.
            # That is faster - ffmpeg's AAC encoder is single-threaded, so one concat
            # encode used a single core however many the machine had - and it is also
            # more robust, because a chapter that does not match the others is
            # normalised by its own encode instead of being dropped at the join.
            workers = max(1, min(len(files), os.cpu_count() or 1, MAX_ENCODERS))
            report(1,
                   f'Encoding {len(files)} chapter{"" if len(files) == 1 else "s"} at '
                   f'{bitrate}' + (f' on {workers} cores' if workers > 1 else '')
                   + f' ({offset_ms / 60000:.0f} min)...')
            sample_rate, channels = common_format(files, ffmpeg)
            segments, detail = _encode_segments(
                files, workdir, bitrate, ffmpeg, report, cancelled,
                source_durations_ms, workers, sample_rate, channels)
            if segments is None:
                _discard(output_path)
                return False, detail

            # The chapter offsets have to come from the *encoded* segments: an AAC
            # encoder does not hand back exactly the duration it was given, and marks
            # built from the mp3 lengths would drift further out with every chapter.
            lengths = [max(1, int(probe_duration(seg, ffmpeg) * 1000))
                       for seg in segments]
            ok, detail, offset_ms = join(
                segments, lengths, ['-c:a', 'copy'], len(files) + 1,
                f'Joining {len(segments)} chapters into {output_path.name}...')

        if not ok:
            # ffmpeg writes as it goes, so a killed or failed run leaves a truncated
            # .m4b behind. Leaving that on disk is worse than not merging at all: it
            # looks like a finished book and plays as a broken one.
            _discard(output_path)
            return False, detail

        report(len(files) + 1, f'Checking {output_path.name}...')
        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, 'ffmpeg produced no output'

        report(total_steps, f'Merged into {output_path.name}')
        total_minutes = offset_ms / 60000
        how = ('by copying, no re-encoding' if copy_only
               else f'at {bitrate}' + (f' on {workers} cores' if workers > 1 else ''))
        return True, (f'Merged {len(files)} files into {output_path.name} '
                      f'{how} ({total_minutes:.0f} min)')

    except subprocess.TimeoutExpired:
        return False, 'ffmpeg timed out'
    except OSError as exc:
        return False, f'Merge failed: {exc}'
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# More encoders than this stops helping and starts thrashing the disk; a 200-chapter
# book does not want 200 ffmpeg processes.
MAX_ENCODERS = 8


def _metadata_text(title: str, author: str, names: List[str],
                   durations_ms: List[int]) -> str:
    """An ffmetadata file: book tags, then one chapter per file at its own offset."""
    lines = [';FFMETADATA1']
    if title:
        lines.append(f'title={_escape_meta(title)}')
    if author:
        lines.append(f'artist={_escape_meta(author)}')
        lines.append(f'album_artist={_escape_meta(author)}')

    offset_ms = 0
    for index, duration_ms in enumerate(durations_ms):
        lines += ['[CHAPTER]', 'TIMEBASE=1/1000',
                  f'START={offset_ms}', f'END={offset_ms + duration_ms}',
                  f'title={_escape_meta(names[index])}']
        offset_ms += duration_ms
    return '\n'.join(lines) + '\n'


def _encode_segments(files: List[Path], workdir: Path, bitrate: str, ffmpeg: str,
                     report, cancelled, durations_ms: List[int],
                     workers: int, sample_rate: int,
                     channels: int) -> Tuple[Optional[List[Path]], str]:
    """Encode every chapter to AAC at once, returning the segment files in order.

    ffmpeg's AAC encoder is single-threaded, so the old one-pass concat encode used
    exactly one core however many the machine had, and a twelve-hour book took as long
    as it took. The chapters are already separate files, which makes them already
    separate jobs - so they are encoded concurrently and then joined with ``-c copy``,
    which is pure I/O. On an eight-core machine that is most of an eight-fold speedup.

    The bar moves one step per chapter *finished*, which is the only claim about
    progress that is true at the moment it is made. How far into the chapters
    currently under the encoder we are goes in the message instead, where a number
    that moves smoothly is worth having and a number that is slightly optimistic
    costs nothing.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    done_ms = [0] * len(files)
    finished = [False] * len(files)
    lock = threading.Lock()
    segments = [workdir / f'{index:05d}.m4a' for index in range(len(files))]
    total_ms = sum(durations_ms)

    def announce(index: int) -> None:
        with lock:
            complete = sum(finished)
            audio = min(1.0, sum(done_ms) / total_ms) if total_ms else 0.0
        # Step 1 was preparing, so chapter one finishing is step 2. The bar advances
        # on `audio` - the share of the book's measured duration that has actually
        # been through the encoder - and not on `complete`, so it moves at exactly the
        # rate the percentage in the message does. `complete` is never behind what the
        # bar shows, because a chapter cannot finish before its audio has.
        report(1 + audio * len(files),
               f'Encoded {complete} of {len(files)} chapters'
               + (f' on {workers} cores' if workers > 1 else '')
               + f'  -  {audio:.0%} of the audio')

    def encode(index: int) -> Tuple[bool, str]:
        def on_time(value_ms: float) -> None:
            # Clamped to the source length so a segment reporting slightly past its
            # own end cannot push the total over 100%.
            with lock:
                done_ms[index] = min(int(value_ms), durations_ms[index])
            announce(index)

        command = [
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-progress', 'pipe:1', '-nostats', '-i', str(files[index]),
            '-map_metadata', '-1', '-c:a', 'aac', '-b:a', bitrate,
            # Every segment to the same format, or the concat that joins them drops
            # the odd one out silently - see common_format.
            '-ar', str(sample_rate), '-ac', str(channels), '-vn',
            '-f', 'mp4', str(segments[index]),
        ]
        ok, detail = _run_streaming(command, cancelled, on_time)
        if ok:
            with lock:
                finished[index] = True
                done_ms[index] = durations_ms[index]
            announce(index)
        return ok, detail

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(encode, range(len(files))))

    for index, (ok, detail) in enumerate(results):
        if not ok:
            return None, detail or f'Could not encode {files[index].name}'
    missing = [seg for seg in segments if not seg.exists() or seg.stat().st_size == 0]
    if missing:
        return None, f'ffmpeg produced no output for {missing[0].name}'
    return segments, ''


def _run_streaming(command: List[str], cancelled,
                   on_time_ms: Optional[Callable[[float], None]] = None
                   ) -> Tuple[bool, str]:
    """Run one ffmpeg, feeding its ``-progress`` timestamps to `on_time_ms`.

    Also the only place cancellation can bite during an encode: a two-minute ffmpeg
    run has to be killed, not politely marked as unwanted once it has finished.
    """
    creation = 0
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        creation = subprocess.CREATE_NO_WINDOW      # no console flash on Windows

    import threading

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=1, creationflags=creation)

    # stderr has to be drained *while* stdout is being read, not after it. A run that
    # produces more diagnostics than the pipe buffer holds otherwise blocks writing to
    # stderr, which stops it writing progress to stdout, which stops us reading - and
    # the merge hangs forever with the bar frozen. A talkative failure is exactly when
    # that happens, so the deadlock only ever showed up on the runs that went wrong.
    errors: List[str] = []

    def drain() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            errors.append(line)
            del errors[:-40]        # only the tail is ever reported

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    try:
        for line in process.stdout:
            if cancelled():
                _terminate_tree(process)
                return False, 'Cancelled - the part-written file was discarded'
            key, _, value = line.strip().partition('=')
            if key == 'out_time_ms' and on_time_ms is not None:
                try:
                    on_time_ms(int(value) / 1000.0)  # microseconds, despite the name
                except ValueError:
                    continue
    finally:
        if process.stdout is not None:
            process.stdout.close()

    code = process.wait(timeout=60)
    reader.join(timeout=10)
    if process.stderr is not None:
        process.stderr.close()
    if code != 0:
        detail = ''.join(errors).strip().splitlines()
        return False, f'ffmpeg failed: {detail[-1] if detail else "unknown error"}'
    return True, ''


def _run_with_progress(command: List[str], lengths: List[int], name: str,
                       report, cancelled, start: float) -> Tuple[bool, str]:
    """One ffmpeg run, reported as the chapter it has reached.

    `lengths` is the duration of each chapter in order, so the timestamp ffmpeg
    prints can be turned back into "chapter 7 of 12" - the same unit every other
    phase counts in. An empty `lengths` means this run is not the chapter work
    itself (the final join after a parallel encode), and it holds the bar at `start`
    while saying how far through it is.
    """
    total_ms = sum(lengths)
    boundaries: List[int] = []
    running = 0
    for length in lengths:
        running += length
        boundaries.append(running)

    def on_time(done_ms: float) -> None:
        if total_ms <= 0:
            report(start, f'Writing {name}...')
            return
        fraction = min(1.0, done_ms / total_ms)
        # How many chapter boundaries the writer has passed - what the message says.
        # The bar itself runs on the fraction, so it keeps moving inside a chapter
        # instead of standing still until the next boundary goes past.
        complete = sum(1 for edge in boundaries if done_ms >= edge)
        report(start + fraction * len(lengths),
               f'Written {complete} of {len(lengths)} chapters into {name}'
               f'  -  {fraction:.0%}')

    return _run_streaming(command, cancelled, on_time)


def _discard(path: Path) -> None:
    """Delete a part-written output, allowing for Windows releasing handles late.

    The killed encoder's file handle is not always closed by the time its process is
    reaped, so the first unlink can lose a race it will win a moment later.
    """
    for attempt in range(6):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if attempt == 5:
                logger.warning('Could not remove the part-written %s: %s', path, exc)
                return
            time.sleep(0.25)


def _terminate_tree(process: 'subprocess.Popen') -> None:
    """Kill a process *and its children*, then wait for it.

    ``Popen.kill()`` alone is not enough on Windows, and the reason is not academic:
    installed via Chocolatey (or Scoop, or any .bat wrapper), "ffmpeg" is a tiny shim
    that launches the real encoder as a child. Killing the shim orphans the encoder,
    which carries on writing the .m4b for the next hour, keeps the output file locked
    so it cannot even be cleaned up, and makes Cancel look like it did nothing.
    """
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # /T takes the tree, /F does not ask nicely. Nothing useful to say if it
        # fails - the fallback kill below is the answer either way.
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug('taskkill on %s failed: %s', process.pid, exc)
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning('ffmpeg (pid %s) did not exit after being killed', process.pid)


def _escape_meta(text: str) -> str:
    """Escape the characters ffmetadata treats as syntax."""
    return re.sub(r'([=;#\\\n])', r'\\\1', str(text))
