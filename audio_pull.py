#!/usr/bin/env python3
"""
audio_pull — Bulk YouTube audio downloader with optional iPhone ringtone export.

Requires:
    pip install yt-dlp tqdm pathvalidate
    ffmpeg binary on PATH (https://ffmpeg.org/download.html)

Usage:
    python audio_pull.py URL [URL ...]
    python audio_pull.py --file urls.txt
    python audio_pull.py --ringtone --start 00:01:00 --duration 30 URL
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from core import (
    RINGTONE_ALL_SEGMENTS,
    RINGTONE_MAX_SECONDS,
    audio_duration,
    build_ydl_opts,
    download_one,
    timestamp_to_seconds,
    to_ringtone,
)

# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
RINGTONES_DIR = DOWNLOADS_DIR / "ringtones"
ARCHIVE_FILE  = DOWNLOADS_DIR / ".yt_archive"  # tracks downloaded video IDs

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

logger = logging.getLogger("audio_pull")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ──────────────────────────────────────────────────────────────
# Batch orchestration (CLI-specific — uses tqdm progress bars)
# ──────────────────────────────────────────────────────────────

def run_downloads(
    urls: list[str],
    audio_format: str,
    workers: int,
    embed_metadata: bool,
) -> list[tuple[str, bool, str, Optional[Path]]]:
    """Download all URLs concurrently, streaming progress via tqdm."""
    ydl_opts = build_ydl_opts(DOWNLOADS_DIR, audio_format, embed_metadata, ARCHIVE_FILE)
    results: list[tuple[str, bool, str, Optional[Path]]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_one, url, ydl_opts): url for url in urls}
        with tqdm(total=len(urls), desc="Downloading", unit="track", leave=True) as pbar:
            for future in as_completed(futures):
                url, ok, msg, path = future.result()
                results.append((url, ok, msg, path))
                tqdm.write(f"  {'✓' if ok else '✗'}  {msg}")
                pbar.update(1)

    return results


def run_ringtone_pass(
    results: list[tuple[str, bool, str, Optional[Path]]],
    start: str,
    duration: int,
    all_segments: bool = False,
) -> None:
    """Convert successfully downloaded files to .m4r ringtones.

    When `all_segments` is True, generates three clips per track using
    RINGTONE_ALL_SEGMENTS instead of a single clip from `start`.
    """
    convertible = [
        (title, path)
        for _, ok, title, path in results
        if ok and path and path.exists()
    ]
    if not convertible:
        logger.warning("No downloaded files found to convert to ringtones.")
        return

    segments = RINGTONE_ALL_SEGMENTS if all_segments else [(None, start)]
    clip_count = len(convertible) * len(segments)
    logger.info("Converting %d clip(s) across %d file(s) …", clip_count, len(convertible))

    for title, source in tqdm(convertible, desc="Ringtones", unit="track", leave=True):
        file_dur = audio_duration(source)
        for label, seg_start in segments:
            start_secs = timestamp_to_seconds(seg_start)
            if start_secs >= file_dur:
                tqdm.write(
                    f"  ⚠  {title} [{label}]: skipped "
                    f"(track is {file_dur:.0f}s, segment starts at {start_secs:.0f}s)"
                )
                continue
            try:
                out = to_ringtone(source, RINGTONES_DIR, seg_start, duration, label)
                tqdm.write(f"  ✓  {out.name}")
            except RuntimeError as exc:
                tqdm.write(f"  ✗  {title} [{label}]: {exc}")

    logger.info("Ringtones saved → %s", RINGTONES_DIR)
    logger.info(
        "To install: AirDrop the .m4r to your iPhone → accept → "
        "Settings → Sounds & Haptics → Ringtone"
    )


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_pull",
        description=(
            "Bulk YouTube audio downloader. "
            "Saves M4A/MP3/Opus files named after the video title. "
            "Pass --ringtone to also export iPhone-ready .m4r clips."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Download a single video as M4A
  python audio_pull.py https://youtu.be/XXX

  # Download multiple videos
  python audio_pull.py https://youtu.be/XXX https://youtu.be/YYY

  # Download from a text file of URLs
  python audio_pull.py --file urls.txt

  # Download as MP3 with 5 parallel workers
  python audio_pull.py --format mp3 --workers 5 https://youtu.be/XXX

  # Download and export iPhone ringtone starting at 1:00 for 30 seconds
  python audio_pull.py --ringtone --start 00:01:00 --duration 30 https://youtu.be/XXX

  # Convert an already-downloaded file to a ringtone (no re-download)
  python audio_pull.py --ringtone-only "Song Title.m4a"
  python audio_pull.py --ringtone-only "Song Title.m4a" --start 00:01:00 --duration 30

  # Download a full playlist
  python audio_pull.py https://www.youtube.com/playlist?list=PLXXX
        """,
    )

    parser.add_argument("urls", nargs="*", metavar="URL", help="One or more YouTube URLs (videos or playlists)")
    parser.add_argument("--file", "-f", metavar="FILE", help="Text file with one URL per line (lines starting with # are ignored)")
    parser.add_argument("--format", default="m4a", choices=["m4a", "mp3", "opus"], help="Output audio format (default: m4a — YouTube's native codec, no quality loss)")
    parser.add_argument("--workers", "-w", type=int, default=3, metavar="N", help="Max concurrent downloads (default: 3)")
    parser.add_argument("--no-metadata", action="store_true", help="Skip embedding title/artist/uploader metadata tags into the audio file")
    parser.add_argument("--ringtone", action="store_true", help="Export trimmed .m4r iPhone ringtone files to downloads/ringtones/")
    parser.add_argument("--ringtone-only", metavar="FILE", help="Convert an already-downloaded file to a ringtone without re-downloading. Accepts a filename (looked up in downloads/) or a full path.")
    parser.add_argument("--ringtone-all", action="store_true", help="Generate 3 ringtones per track: intro (0:00), mid (0:45), outro (1:30).")
    parser.add_argument("--start", default="00:00:00", metavar="HH:MM:SS", help="Start time for the ringtone clip (default: 00:00:00)")
    parser.add_argument("--duration", type=int, default=30, metavar="SECONDS", help="Ringtone clip length in seconds, max 40 (default: 30)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug-level logging")
    return parser


def _collect_urls(args: argparse.Namespace) -> list[str]:
    """Merge inline URLs with those read from --file."""
    urls = list(args.urls)
    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            logger.error("URL file not found: %s", args.file)
            sys.exit(1)
        lines = fp.read_text(encoding="utf-8").splitlines()
        urls += [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return urls


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    if not shutil.which("ffmpeg"):
        logger.error(
            "ffmpeg not found on PATH. "
            "Install it (e.g. 'winget install Gyan.FFmpeg' on Windows, "
            "'brew install ffmpeg' on macOS) then restart your terminal."
        )
        sys.exit(1)

    # --ringtone-only: convert an existing file, skip any downloading
    if args.ringtone_only:
        source = Path(args.ringtone_only)
        if not source.is_absolute():
            source = DOWNLOADS_DIR / source
        if not source.exists():
            logger.error("File not found: %s", source)
            sys.exit(1)
        if args.duration > RINGTONE_MAX_SECONDS:
            logger.warning(
                "--duration %d exceeds the iPhone 40-second limit; clamping to %d.",
                args.duration, RINGTONE_MAX_SECONDS,
            )
            args.duration = RINGTONE_MAX_SECONDS
        segments = RINGTONE_ALL_SEGMENTS if args.ringtone_all else [(None, args.start)]
        file_dur = audio_duration(source)
        for label, seg_start in segments:
            start_secs = timestamp_to_seconds(seg_start)
            if start_secs >= file_dur:
                logger.warning(
                    "Skipping [%s]: track is %.0fs, segment starts at %.0fs",
                    label or "clip", file_dur, start_secs,
                )
                continue
            try:
                out = to_ringtone(source, RINGTONES_DIR, seg_start, args.duration, label)
                logger.info("Ringtone saved → %s", out)
            except RuntimeError as exc:
                logger.error("Conversion failed [%s]: %s", label or "clip", exc)
                sys.exit(1)
        logger.info(
            "To install: AirDrop the .m4r to your iPhone → accept → "
            "Settings → Sounds & Haptics → Ringtone"
        )
        return

    urls = _collect_urls(args)
    if not urls:
        parser.print_help()
        sys.exit(1)

    if (args.ringtone or args.ringtone_all) and args.duration > RINGTONE_MAX_SECONDS:
        logger.warning(
            "--duration %d exceeds the iPhone 40-second limit; clamping to %d.",
            args.duration, RINGTONE_MAX_SECONDS,
        )
        args.duration = RINGTONE_MAX_SECONDS

    DOWNLOADS_DIR.mkdir(exist_ok=True)
    ARCHIVE_FILE.touch()  # create if missing; yt-dlp appends to it on each run

    logger.info(
        "Starting: %d URL(s)  format=%s  workers=%d  metadata=%s",
        len(urls), args.format, args.workers, not args.no_metadata,
    )

    results = run_downloads(
        urls=urls,
        audio_format=args.format,
        workers=args.workers,
        embed_metadata=not args.no_metadata,
    )

    succeeded = [r for r in results if r[1]]
    failed    = [r for r in results if not r[1]]
    logger.info("Done: %d succeeded, %d failed", len(succeeded), len(failed))

    for _, _, msg, _ in failed:
        logger.error("  FAILED: %s", msg)

    if args.ringtone or args.ringtone_all:
        run_ringtone_pass(succeeded, args.start, args.duration, all_segments=args.ringtone_all)


if __name__ == "__main__":
    main()
