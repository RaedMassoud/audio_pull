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

WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "changeme")

# YouTube cookies - upload cookies.txt to the Space's Files tab to unblock downloads.
# See: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp
COOKIES_FILE = Path(__file__).parent / "cookies.txt"

MAX_WORKERS = 2


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
            archive_path=None,
            cookies_file=COOKIES_FILE if COOKIES_FILE.exists() else None,
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
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&display=swap');

/* ── GitHub dark palette ─────────────────────── */
:root {
    --bg:      #0d1117;
    --canvas:  #161b22;
    --overlay: #1c2128;
    --border:  #30363d;
    --border-s:#21262d;
    --blue:    #58a6ff;
    --blue-dim:rgba(88,166,255,0.12);
    --green:   #238636;
    --green-h: #2ea043;
    --text:    #e6edf3;
    --text-2:  #8b949e;
    --text-3:  #484f58;
    --r: 6px;
}

body, .gradio-container {
    background: var(--bg) !important;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif !important;
    color: var(--text) !important;
}
.gradio-container { max-width: 680px !important; }

/* ── Header ─────────────────────────────────── */
#ap-header {
    padding: 28px 0 24px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
    display: flex;
    align-items: baseline;
    gap: 12px;
}
#ap-header h1 {
    font-family: 'Syne', sans-serif !important;
    font-size: 1.35rem !important;
    font-weight: 800 !important;
    color: var(--text) !important;
    margin: 0 !important;
    letter-spacing: -0.02em !important;
}
#ap-header p {
    font-size: 0.88rem !important;
    color: var(--text-2) !important;
    margin: 0 !important;
}

/* ── Section labels ──────────────────────────── */
.ap-label {
    font-size: 0.82rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 6px;
}

/* ── Inputs ─────────────────────────────────── */
.gradio-container textarea,
.gradio-container input[type="text"] {
    background: var(--canvas) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--r) !important;
    color: var(--text) !important;
    font-size: 0.9rem !important;
    line-height: 1.6 !important;
    transition: border-color .12s !important;
}
.gradio-container textarea:focus,
.gradio-container input[type="text"]:focus {
    border-color: var(--blue) !important;
    box-shadow: 0 0 0 3px var(--blue-dim) !important;
    outline: none !important;
}

/* ── Radio pills ─────────────────────────────── */
.gradio-container .wrap {
    gap: 6px !important;
    flex-wrap: wrap !important;
}
.gradio-container .wrap label {
    background: var(--canvas) !important;
    border: 1px solid var(--border) !important;
    border-radius: 100px !important;
    padding: 5px 14px !important;
    font-size: 0.84rem !important;
    font-weight: 500 !important;
    color: var(--text-2) !important;
    cursor: pointer !important;
    transition: all .12s !important;
    white-space: nowrap !important;
}
.gradio-container .wrap label:hover {
    border-color: var(--blue) !important;
    color: var(--blue) !important;
    background: var(--blue-dim) !important;
}
.gradio-container .wrap label.selected {
    background: var(--blue-dim) !important;
    border-color: var(--blue) !important;
    color: var(--blue) !important;
    font-weight: 600 !important;
}

/* ── Slider ─────────────────────────────────── */
input[type="range"] { accent-color: var(--blue) !important; }

/* ── Button ─────────────────────────────────── */
.gradio-container button.primary {
    background: var(--green) !important;
    color: #fff !important;
    font-weight: 600 !important;
    font-size: 0.9rem !important;
    border: 1px solid rgba(240,246,252,0.1) !important;
    border-radius: var(--r) !important;
    padding: 10px 20px !important;
    width: 100% !important;
    cursor: pointer !important;
    transition: background .12s !important;
}
.gradio-container button.primary:hover {
    background: var(--green-h) !important;
}

/* ── Labels / info ──────────────────────────── */
.gradio-container label > span,
.gradio-container .label-wrap > span {
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    color: var(--text) !important;
}
.gradio-container .info {
    font-size: 0.76rem !important;
    color: var(--text-2) !important;
}

/* ── Block wrappers ─────────────────────────── */
.gradio-container .block {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
}

/* ── Accordion ──────────────────────────────── */
.gradio-container .accordion {
    background: var(--canvas) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--r) !important;
    overflow: hidden !important;
}

/* ── Tabs ───────────────────────────────────── */
.gradio-container .tabs > .tab-nav {
    border-bottom: 1px solid var(--border) !important;
    background: transparent !important;
}
.gradio-container .tab-nav button {
    font-size: 0.86rem !important;
    font-weight: 500 !important;
    color: var(--text-2) !important;
    background: transparent !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    padding: 10px 0 !important;
    margin-right: 16px !important;
    transition: color .12s !important;
}
.gradio-container .tab-nav button:hover { color: var(--text) !important; }
.gradio-container .tab-nav button.selected {
    color: var(--text) !important;
    border-bottom-color: var(--blue) !important;
    font-weight: 600 !important;
}

/* ── File / log panels ──────────────────────── */
.gradio-container .file-preview {
    background: var(--canvas) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--r) !important;
}
#ap-log textarea {
    background: var(--canvas) !important;
    color: var(--text-2) !important;
    font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace !important;
    font-size: 0.76rem !important;
    line-height: 1.8 !important;
}

/* ── Remove Gradio branding ─────────────────── */
footer { display: none !important; }
.built-with { display: none !important; }

/* ── Scrollbar ──────────────────────────────── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
"""

_THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.blue,
    neutral_hue=gr.themes.colors.slate,
    font=["system-ui", "-apple-system", "sans-serif"],
    font_mono=["ui-monospace", "monospace"],
)

with gr.Blocks(title="audio pull", theme=_THEME, css=_CSS) as demo:

    gr.HTML("""
    <div id="ap-header">
        <h1>audio pull</h1>
        <p>Download YouTube audio as M4A, MP3, or Opus. Playlists supported.</p>
    </div>
    """)

    gr.HTML('<p class="ap-label">YouTube URLs</p>')
    urls_box = gr.Textbox(
        placeholder=(
            "https://youtu.be/...\n"
            "https://youtu.be/...\n\n"
            "One link per line. Playlist links grab every song at once."
        ),
        lines=5,
        show_label=False,
    )

    gr.HTML('<p class="ap-label" style="margin-top:16px">Format</p>')
    fmt = gr.Radio(
        choices=[
            ("Best quality  (M4A)", "M4A"),
            ("Works everywhere  (MP3)", "MP3"),
            ("Smallest file  (Opus)", "Opus"),
        ],
        value="M4A",
        show_label=False,
        info="Not sure? M4A is YouTube's native format - no quality loss.",
    )

    gr.HTML('<div style="height:12px"></div>')
    with gr.Accordion("iPhone Ringtone Export (.m4r)", open=False):
        gr.HTML("""<p style="font-size:0.86rem;color:#8b949e;margin:4px 0 16px;line-height:1.6">
            Exports a trimmed .m4r clip. AirDrop it to your iPhone and it appears in
            <strong style="color:#c9d1d9">Settings - Sounds &amp; Haptics - Ringtone</strong>.
            Max 40 seconds.
        </p>""")
        ringtone_mode = gr.Radio(
            choices=["None", "Single clip", "All 3 (intro / mid / outro)"],
            value="None",
            label="Ringtone type",
            info="Single: choose start + length  -  All 3: auto intro (0:00) / mid (0:45) / outro (1:30)",
        )
        with gr.Row(visible=False) as clip_controls:
            start_time = gr.Textbox(
                value="0:00",
                label="Start at",
                placeholder="e.g. 0:45 or 1:30",
                info="Where in the song to begin",
                scale=1,
            )
            duration = gr.Slider(
                minimum=10, maximum=40, value=30, step=1,
                label="Length (seconds)",
                info="Max 40s - iPhone limit",
                scale=3,
            )

    gr.HTML('<div style="height:16px"></div>')
    btn = gr.Button("Download", variant="primary")

    gr.HTML('<div style="height:24px"></div>')
    with gr.Tabs():
        with gr.Tab("Your files"):
            file_out = gr.File(file_count="multiple", show_label=False)
        with gr.Tab("Log"):
            log_out = gr.Textbox(
                lines=12,
                interactive=False,
                show_copy_button=True,
                show_label=False,
                elem_id="ap-log",
                placeholder="Activity log appears here after downloading.",
            )

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

demo.queue()
demo.launch(auth=(WEB_USERNAME, WEB_PASSWORD), ssr_mode=False, show_api=False)
