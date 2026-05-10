"""
app.py - Gradio web interface for audio_pull.
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

# Read credentials from Space secrets - set these in your HuggingFace Space settings
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
) -> tuple[str, Optional[list[str]]]:
    """
    Download audio and optionally export ringtones.
    Returns (log_text, list_of_file_paths).
    Each call gets its own temp directory - no state shared between sessions.
    """
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)
        logger.info(msg)

    # Parse URLs - one per line, # lines ignored
    urls = [
        line.strip()
        for line in urls_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not urls:
        return "No URLs provided.", None

    duration = min(int(duration), RINGTONE_MAX_SECONDS)
    fmt = audio_format.lower()  # Radio value is "M4A"/"MP3"/"Opus"; yt-dlp wants lowercase

    log(f"Starting {len(urls)} download(s)…")

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
                display = msg if ok else msg.splitlines()[0].removeprefix("ERROR: ").strip()
                log(f"  {'✓' if ok else '✗'}  {display}")
                progress(i / len(urls), desc=f"Downloading {i}/{len(urls)}…")

        succeeded = [(title, path) for _, ok, title, path in results if ok and path and path.exists()]
        failed    = [msg for _, ok, msg, _ in results if not ok]

        if failed:
            log(f"\n⚠  {len(failed)} couldn't be downloaded:")
            for msg in failed:
                # yt-dlp errors are verbose; show only the first meaningful line
                clean = msg.splitlines()[0].removeprefix("ERROR: ").strip()
                log(f"  {clean}")

        # ── Ringtone phase ────────────────────────────────────
        if ringtone_mode != "None" and succeeded:
            all_segs = ringtone_mode == "All 3 (intro / mid / outro)"
            try:
                start_secs_check = timestamp_to_seconds(start_time)
            except (ValueError, IndexError):
                log(f"\n⚠  Invalid start time '{start_time}' — use a format like 0:45 or 1:30. Ringtone skipped.")
                start_secs_check = None

            if start_secs_check is not None:
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

        # ── Collect output files ──────────────────────────────
        if not succeeded:
            return "\n".join(log_lines), None

        # Copy each file to its own temp path so they survive session_dir cleanup
        output_files: list[str] = []
        for src in sorted(audio_dir.iterdir()):
            if src.is_file():
                fd, dst = tempfile.mkstemp(suffix=src.suffix)
                os.close(fd)
                shutil.copy2(src, dst)
                output_files.append(dst)
        ringtone_count = 0
        if ringtone_dir.exists():
            for src in sorted(ringtone_dir.iterdir()):
                if src.is_file():
                    fd, dst = tempfile.mkstemp(suffix=src.suffix)
                    os.close(fd)
                    shutil.copy2(src, dst)
                    output_files.append(dst)
                    ringtone_count += 1

        summary = f"\n✓  {len(succeeded)} song(s) ready to download"
        if ringtone_count:
            summary += f"  +  {ringtone_count} ringtone(s)"
        if failed:
            summary += f"  ·  {len(failed)} failed"
        log(summary)

        return "\n".join(log_lines), output_files

    finally:
        shutil.rmtree(session_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# Gradio UI
# ──────────────────────────────────────────────────────────────

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=IBM+Plex+Mono:ital,wght@0,300;0,400;0,500;1,300&display=swap');

/* ── Layout ───────────────────────────────────── */
.gradio-container { max-width: 1020px !important; }

/* ── Header ───────────────────────────────────── */
#ap-header {
    padding: 6px 0 26px;
    border-bottom: 1px solid #1f1f1f;
    margin-bottom: 28px;
    user-select: none;
}
#ap-header h1 {
    font-family: 'Syne', sans-serif !important;
    font-size: 2.4rem !important;
    font-weight: 800 !important;
    letter-spacing: -0.05em !important;
    color: #efefef !important;
    margin: 0 0 7px !important;
    line-height: 1 !important;
}
#ap-header h1 span { color: #f0a500; }
#ap-header p {
    font-family: 'IBM Plex Mono', monospace !important;
    color: #444 !important;
    font-size: 0.76rem !important;
    margin: 0 !important;
    letter-spacing: 0.06em !important;
}

/* ── Log ──────────────────────────────────────── */
#ap-log textarea {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.72rem !important;
    line-height: 1.9 !important;
}

/* ── Tip box ──────────────────────────────────── */
#ap-tip {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: #3a3a3a;
    padding: 14px 0 0;
    line-height: 1.7;
}
"""

_THEME = gr.themes.Base(
    primary_hue="amber",
    neutral_hue="neutral",
    font=[gr.themes.GoogleFont("IBM Plex Mono"), "monospace"],
    font_mono=[gr.themes.GoogleFont("IBM Plex Mono"), "monospace"],
)

with gr.Blocks(title="audio pull", theme=_THEME, css=_CSS) as demo:

    gr.HTML("""
    <div id="ap-header">
        <h1>audio<span>.</span>pull</h1>
        <p>youtube → m4a &nbsp;/&nbsp; mp3 &nbsp;/&nbsp; opus &nbsp;&nbsp;·&nbsp;&nbsp; iphone ringtone export (.m4r)</p>
    </div>
    """)

    with gr.Row(equal_height=False):

        # ── Left: inputs ───────────────────────────
        with gr.Column(scale=5):
            urls_box = gr.Textbox(
                label="YouTube URLs",
                placeholder=(
                    "https://youtu.be/...\n"
                    "https://youtu.be/...\n\n"
                    "# one URL per line - # lines are ignored\n"
                    "# playlist URLs grab the whole playlist"
                ),
                lines=9,
            )

            fmt = gr.Radio(
                choices=[
                    ("Best quality  (M4A)", "M4A"),
                    ("Works everywhere  (MP3)", "MP3"),
                    ("Smallest file  (Opus)", "Opus"),
                ],
                value="M4A",
                label="Audio quality",
                info="Not sure? Leave it on Best quality.",
            )

            with gr.Accordion("iPhone Ringtone Export  (.m4r)", open=False):
                gr.HTML("""<p style="font-family:'IBM Plex Mono',monospace;font-size:0.72rem;
                            color:#555;margin:0 0 14px;line-height:1.7">
                    Ringtones are trimmed AAC clips in .m4r format - just AirDrop to your iPhone
                    and they appear instantly in Settings → Sounds &amp; Haptics → Ringtone.
                    Max 40 seconds (iPhone limit).
                </p>""")
                ringtone_mode = gr.Radio(
                    choices=["None", "Single clip", "All 3 (intro / mid / outro)"],
                    value="None",
                    label="Mode",
                    info=(
                        "Single clip - one .m4r starting at your chosen time  ·  "
                        "All 3 - auto-generates intro (0:00), mid (0:45), outro (1:30)"
                    ),
                )
                with gr.Row(visible=False) as clip_controls:
                    start_time = gr.Textbox(
                        value="0:00",
                        label="Start at",
                        placeholder="e.g. 0:45 or 1:30",
                        info="Where in the song to start the clip",
                        scale=1,
                    )
                    duration = gr.Slider(
                        minimum=10, maximum=40, value=30, step=1,
                        label="Length (seconds)",
                        info="Max 40 seconds - iPhone limit",
                        scale=3,
                    )

            btn = gr.Button("↓  Download", variant="primary", size="lg")

            gr.HTML("""<div id="ap-tip">
                tip: paste a playlist URL to grab every track at once
            </div>""")

        # ── Right: output ──────────────────────────
        with gr.Column(scale=5):
            log_out = gr.Textbox(
                label="Log",
                lines=21,
                interactive=False,
                show_copy_button=True,
                elem_id="ap-log",
            )
            file_out = gr.File(label="Your files  (tap any file to download)", file_count="multiple")

    def _toggle_clip_controls(mode: str):
        return gr.update(visible=(mode == "Single clip"))

    ringtone_mode.change(
        fn=_toggle_clip_controls,
        inputs=[ringtone_mode],
        outputs=[clip_controls],
    )

    btn.click(
        fn=process,
        inputs=[urls_box, fmt, ringtone_mode, start_time, duration],
        outputs=[log_out, file_out],
    )

demo.launch(auth=(WEB_USERNAME, WEB_PASSWORD), ssr_mode=False)
