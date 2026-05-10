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
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Syne:wght@700;800&display=swap');

/* ── Globals ─────────────────────────────────── */
:root {
    --bg:     #f0ede8;
    --card:   #ffffff;
    --border: #e2ddd6;
    --accent: #ff6b35;
    --accent-dim: rgba(255,107,53,0.08);
    --text:   #1c1917;
    --text-2: #6b6158;
    --text-3: #b5ada4;
    --r:   16px;
    --r-sm: 10px;
    --sh: 0 1px 3px rgba(0,0,0,.05), 0 4px 14px rgba(0,0,0,.05);
}

body, .gradio-container {
    background: var(--bg) !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
}
.gradio-container { max-width: 680px !important; }

/* ── Header ─────────────────────────────────── */
#ap-header {
    text-align: center;
    padding: 52px 0 40px;
}
#ap-logo {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 100px;
    padding: 5px 16px 5px 7px;
    margin-bottom: 24px;
    box-shadow: var(--sh);
}
#ap-logo-dot {
    width: 28px; height: 28px;
    background: var(--accent);
    border-radius: 50%;
    font-size: 15px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: #fff;
}
#ap-logo-name {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 0.82rem;
    color: var(--text);
}
#ap-header h1 {
    font-family: 'Syne', sans-serif !important;
    font-size: 3rem !important;
    font-weight: 800 !important;
    letter-spacing: -0.04em !important;
    color: var(--text) !important;
    line-height: 1.08 !important;
    margin: 0 0 16px !important;
}
#ap-header p {
    font-size: 1.05rem !important;
    color: var(--text-2) !important;
    margin: 0 !important;
    line-height: 1.55 !important;
}

/* ── Step labels ─────────────────────────────── */
.ap-step {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-3);
    margin: 0 0 8px;
    padding-left: 2px;
}

/* ── Inputs ─────────────────────────────────── */
.gradio-container textarea,
.gradio-container input[type="text"] {
    background: var(--card) !important;
    border: 1.5px solid var(--border) !important;
    border-radius: var(--r-sm) !important;
    color: var(--text) !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-size: 0.92rem !important;
    line-height: 1.65 !important;
    box-shadow: var(--sh) !important;
    transition: border-color .15s, box-shadow .15s !important;
}
.gradio-container textarea:focus,
.gradio-container input[type="text"]:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-dim), var(--sh) !important;
    outline: none !important;
}

/* ── Radio pills ─────────────────────────────── */
.gradio-container .wrap {
    gap: 8px !important;
    flex-wrap: wrap !important;
}
.gradio-container .wrap label {
    background: var(--card) !important;
    border: 1.5px solid var(--border) !important;
    border-radius: 100px !important;
    padding: 9px 22px !important;
    font-size: 0.88rem !important;
    font-weight: 500 !important;
    color: var(--text-2) !important;
    cursor: pointer !important;
    transition: all .15s !important;
    box-shadow: var(--sh) !important;
    white-space: nowrap !important;
}
.gradio-container .wrap label:hover {
    border-color: var(--accent) !important;
    color: var(--accent) !important;
    background: var(--accent-dim) !important;
}
.gradio-container .wrap label.selected {
    background: var(--accent-dim) !important;
    border-color: var(--accent) !important;
    color: var(--accent) !important;
    font-weight: 700 !important;
}

/* ── Slider ─────────────────────────────────── */
input[type="range"] { accent-color: var(--accent) !important; }

/* ── Button ─────────────────────────────────── */
.gradio-container button.primary {
    background: var(--accent) !important;
    color: #fff !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-weight: 700 !important;
    font-size: 1.05rem !important;
    border: none !important;
    border-radius: 100px !important;
    padding: 18px 40px !important;
    width: 100% !important;
    cursor: pointer !important;
    transition: all .15s ease !important;
    box-shadow: 0 4px 18px rgba(255,107,53,.35) !important;
    letter-spacing: -0.01em !important;
}
.gradio-container button.primary:hover {
    background: #e85a28 !important;
    box-shadow: 0 6px 26px rgba(255,107,53,.45) !important;
    transform: translateY(-1px) !important;
}
.gradio-container button.primary:active {
    transform: translateY(0) !important;
}

/* ── Labels / info ──────────────────────────── */
.gradio-container label > span,
.gradio-container .label-wrap > span {
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    color: var(--text-2) !important;
}
.gradio-container .info {
    font-size: 0.75rem !important;
    color: var(--text-3) !important;
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
    background: var(--card) !important;
    border: 1.5px solid var(--border) !important;
    border-radius: var(--r) !important;
    box-shadow: var(--sh) !important;
    overflow: hidden !important;
}

/* ── Tabs ───────────────────────────────────── */
.gradio-container .tabs > .tab-nav {
    border-bottom: 1.5px solid var(--border) !important;
    background: transparent !important;
    margin-bottom: 16px !important;
}
.gradio-container .tab-nav button {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-size: 0.88rem !important;
    font-weight: 600 !important;
    color: var(--text-3) !important;
    background: transparent !important;
    border: none !important;
    border-bottom: 2.5px solid transparent !important;
    padding: 12px 4px !important;
    margin-right: 20px !important;
    transition: color .15s !important;
}
.gradio-container .tab-nav button:hover { color: var(--text-2) !important; }
.gradio-container .tab-nav button.selected {
    color: var(--accent) !important;
    border-bottom-color: var(--accent) !important;
}

/* ── File component ─────────────────────────── */
.gradio-container .file-preview {
    background: var(--card) !important;
    border: 1.5px solid var(--border) !important;
    border-radius: var(--r) !important;
    box-shadow: var(--sh) !important;
}

/* ── Log ────────────────────────────────────── */
#ap-log textarea {
    font-size: 0.76rem !important;
    line-height: 1.85 !important;
    color: var(--text-2) !important;
    font-family: monospace !important;
    background: var(--card) !important;
}

/* ── Footer ─────────────────────────────────── */
#ap-footer {
    text-align: center;
    padding: 32px 0 48px;
    font-size: 0.76rem;
    color: var(--text-3);
    line-height: 1.7;
}
#ap-footer a { color: var(--text-3); }

/* ── Scrollbar ──────────────────────────────── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
"""

_THEME = gr.themes.Base(
    primary_hue="orange",
    neutral_hue="stone",
    font=[gr.themes.GoogleFont("Plus Jakarta Sans"), "sans-serif"],
    font_mono=["monospace"],
)

with gr.Blocks(title="audio pull", theme=_THEME, css=_CSS) as demo:

    # ── Header ─────────────────────────────────────────────────
    gr.HTML("""
    <div id="ap-header">
        <div id="ap-logo">
            <div id="ap-logo-dot">♪</div>
            <span id="ap-logo-name">audio.pull</span>
        </div>
        <h1>YouTube audio,<br>in seconds.</h1>
        <p>Paste any YouTube link and get the audio file.<br>
           No account, no software, no fuss.</p>
    </div>
    """)

    # ── Step 1: URLs ────────────────────────────────────────────
    gr.HTML('<p class="ap-step">Paste your links</p>')
    urls_box = gr.Textbox(
        placeholder=(
            "https://youtu.be/...\n"
            "https://youtu.be/...\n\n"
            "Paste one link per line.\n"
            "Playlist links work too - paste one to grab every song."
        ),
        lines=5,
        show_label=False,
    )

    # ── Step 2: Format ──────────────────────────────────────────
    gr.HTML('<p class="ap-step" style="margin-top:20px">Choose a format</p>')
    fmt = gr.Radio(
        choices=[
            ("Best quality  (M4A)", "M4A"),
            ("Works everywhere  (MP3)", "MP3"),
            ("Smallest file  (Opus)", "Opus"),
        ],
        value="M4A",
        show_label=False,
        info="Not sure? Leave it on Best quality - it's the same audio YouTube streams.",
    )

    # ── Optional: Ringtones ─────────────────────────────────────
    gr.HTML('<div style="height:12px"></div>')
    with gr.Accordion("Make iPhone Ringtones  (.m4r)  - optional", open=False):
        gr.HTML("""
        <p style="font-family:'Plus Jakarta Sans',sans-serif;font-size:0.88rem;
                  color:#6b6158;margin:4px 0 18px;line-height:1.65">
            Turns any downloaded song into an iPhone ringtone. AirDrop the
            .m4r file to your iPhone - it shows up instantly in
            <strong>Settings - Sounds &amp; Haptics - Ringtone</strong>.
            Max 40 seconds.
        </p>""")
        ringtone_mode = gr.Radio(
            choices=["None", "Single clip", "All 3 (intro / mid / outro)"],
            value="None",
            label="Ringtone type",
            info=(
                "Single clip: choose where in the song to start  -  "
                "All 3: auto-clips intro (0:00), mid-song (0:45), and outro (1:30)"
            ),
        )
        with gr.Row(visible=False) as clip_controls:
            start_time = gr.Textbox(
                value="0:00",
                label="Start at",
                placeholder="e.g. 0:45 or 1:30",
                info="Where in the song to begin the clip",
                scale=1,
            )
            duration = gr.Slider(
                minimum=10, maximum=40, value=30, step=1,
                label="Length (seconds)",
                info="Max 40s - iPhone limit",
                scale=3,
            )

    # ── Download button ─────────────────────────────────────────
    gr.HTML('<div style="height:20px"></div>')
    btn = gr.Button("Download", variant="primary", size="lg")

    # ── Output: files first, log tucked away ────────────────────
    gr.HTML('<div style="height:28px"></div>')
    with gr.Tabs():
        with gr.Tab("Your files"):
            file_out = gr.File(
                file_count="multiple",
                show_label=False,
            )
        with gr.Tab("Activity log"):
            log_out = gr.Textbox(
                lines=14,
                interactive=False,
                show_copy_button=True,
                show_label=False,
                elem_id="ap-log",
                placeholder="Logs will appear here after you hit Download.",
            )

    # ── Footer ──────────────────────────────────────────────────
    gr.HTML("""
    <div id="ap-footer">
        Paste a link. Hit download. That's it.<br>
        Playlists, single videos, and ringtone export all supported.
    </div>
    """)

    # ── Event handlers ──────────────────────────────────────────
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
