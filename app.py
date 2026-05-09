"""
app.py — Gradio web interface for audio_pull.
Designed for deployment on Hugging Face Spaces.

Authentication is set via environment variables in the Space settings:
  WEB_USERNAME  (default: admin)
  WEB_PASSWORD  (default: changeme)
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import gradio as gr

from core import (
    RINGTONE_ALL_SEGMENTS,
    RINGTONE_MAX_SECONDS,
    audio_duration,
    build_ydl_opts,
    download_one,
    timestamp_to_seconds,
    to_ringtone,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger("app")

# Read credentials from Space secrets — set these in your HuggingFace Space settings
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "changeme")

MAX_WORKERS = 2  # conservative for shared HuggingFace CPU


# ──────────────────────────────────────────────────────────────
# Core processing
# ──────────────────────────────────────────────────────────────

def process(
    urls_text: str,
    audio_format: str,
    ringtone_mode: str,
    start_time: str,
    duration: int,
    progress: gr.Progress = gr.Progress(),
) -> tuple[str, Optional[str]]:
    """
    Download audio and optionally export ringtones.
    Returns (log_text, zip_file_path).
    Each call gets its own temp directory — no state shared between sessions.
    """
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)
        logger.info(msg)

    # Parse URLs — one per line, # lines ignored
    urls = [
        line.strip()
        for line in urls_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not urls:
        return "No URLs provided.", None

    duration = min(int(duration), RINGTONE_MAX_SECONDS)
    fmt = audio_format.lower()  # Gradio gives "M4A", yt-dlp wants "m4a"

    log(f"Starting: {len(urls)} URL(s)  format={fmt}")

    # Each session writes to its own temp directory
    session_dir = Path(tempfile.mkdtemp())
    audio_dir   = session_dir / "audio"
    ringtone_dir = session_dir / "ringtones"
    audio_dir.mkdir()

    try:
        ydl_opts = build_ydl_opts(
            output_dir=audio_dir,
            audio_format=fmt,
            embed_metadata=True,
            archive_path=None,  # no duplicate-skip on web; each session is independent
        )

        # ── Download phase ────────────────────────────────────
        results: list[tuple[str, bool, str, Optional[Path]]] = []
        progress(0, desc="Downloading…")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(download_one, url, ydl_opts): url for url in urls}
            for i, future in enumerate(as_completed(futures), 1):
                url, ok, msg, path = future.result()
                results.append((url, ok, msg, path))
                log(f"  {'✓' if ok else '✗'}  {msg}")
                progress(i / len(urls), desc=f"Downloading {i}/{len(urls)}…")

        succeeded = [(title, path) for _, ok, title, path in results if ok and path and path.exists()]
        failed    = [(msg) for _, ok, msg, _ in results if not ok]

        log(f"\n{len(succeeded)} succeeded, {len(failed)} failed")
        for msg in failed:
            log(f"  FAILED: {msg}")

        # ── Ringtone phase ────────────────────────────────────
        if ringtone_mode != "None" and succeeded:
            all_segs = ringtone_mode == "All 3 (intro / mid / outro)"
            segments = RINGTONE_ALL_SEGMENTS if all_segs else [(None, start_time)]
            total_clips = len(succeeded) * len(segments)
            log(f"\nConverting {total_clips} ringtone clip(s)…")
            progress(0, desc="Converting ringtones…")

            for i, (title, source) in enumerate(succeeded, 1):
                file_dur = audio_duration(source)
                for label, seg_start in segments:
                    if timestamp_to_seconds(seg_start) >= file_dur:
                        log(
                            f"  ⚠  {title} [{label}]: skipped "
                            f"(track is {file_dur:.0f}s, segment starts at "
                            f"{timestamp_to_seconds(seg_start):.0f}s)"
                        )
                        continue
                    try:
                        out = to_ringtone(source, ringtone_dir, seg_start, duration, label)
                        log(f"  ✓  {out.name}")
                    except RuntimeError as exc:
                        log(f"  ✗  {title}: {exc}")
                progress(i / len(succeeded), desc=f"Ringtones {i}/{len(succeeded)}…")

        # ── Build zip ─────────────────────────────────────────
        if not succeeded:
            return "\n".join(log_lines), None

        zip_path = Path(tempfile.mkstemp(suffix=".zip")[1])
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in audio_dir.iterdir():
                if f.is_file():
                    zf.write(f, f.name)
            if ringtone_dir.exists():
                for f in ringtone_dir.iterdir():
                    if f.is_file():
                        zf.write(f, f"ringtones/{f.name}")

        log(f"\nReady — {len(list(audio_dir.iterdir()))} audio file(s) in zip.")
        if ringtone_dir.exists():
            log(f"Ringtones folder included in zip.")
        log("Download the zip below, then extract it.")

        return "\n".join(log_lines), str(zip_path)

    finally:
        # Clean up session audio/ringtone files; the zip lives separately in /tmp
        shutil.rmtree(session_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# Gradio UI
# ──────────────────────────────────────────────────────────────

with gr.Blocks(title="audio pull", theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# 🎵 audio pull\n"
        "Download YouTube audio as M4A / MP3 / Opus. "
        "Optionally export iPhone ringtone clips (.m4r)."
    )

    with gr.Row():
        with gr.Column(scale=1):
            urls_box = gr.Textbox(
                label="YouTube URLs",
                placeholder=(
                    "Paste one URL per line:\n"
                    "https://youtu.be/XXX\n"
                    "https://youtu.be/YYY\n\n"
                    "# Lines starting with # are ignored\n"
                    "# Playlist URLs work too"
                ),
                lines=10,
            )
            fmt = gr.Radio(
                choices=["M4A", "MP3", "Opus"],
                value="M4A",
                label="Audio format",
                info="M4A = best quality (YouTube native, no re-encoding loss).  MP3 = universal.  Opus = smallest size.",
            )

            gr.Markdown("### iPhone Ringtone Export")
            ringtone_mode = gr.Radio(
                choices=["None", "Single clip", "All 3 (intro / mid / outro)"],
                value="None",
                label="Ringtone mode",
                info=(
                    "Single clip: one .m4r from --start for --duration seconds.  "
                    "All 3: intro (0:00), mid (0:45), outro (1:30) — 30s each."
                ),
            )
            with gr.Row():
                start_time = gr.Textbox(
                    value="00:00:00",
                    label="Start time (HH:MM:SS)",
                    info="Only used in Single clip mode.",
                    scale=1,
                )
                duration = gr.Slider(
                    minimum=10,
                    maximum=40,
                    value=30,
                    step=1,
                    label="Duration (seconds)",
                    info="Max 40s — iPhone limit.",
                    scale=2,
                )

            btn = gr.Button("Download", variant="primary", size="lg")

        with gr.Column(scale=1):
            log_out  = gr.Textbox(label="Log", lines=22, interactive=False, show_copy_button=True)
            file_out = gr.File(label="Download zip (extract to get your files)")

    btn.click(
        fn=process,
        inputs=[urls_box, fmt, ringtone_mode, start_time, duration],
        outputs=[log_out, file_out],
    )

demo.launch(auth=(WEB_USERNAME, WEB_PASSWORD))
