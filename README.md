---
title: Audio Pull
emoji: 🎵
colorFrom: purple
colorTo: blue
sdk: gradio
sdk_version: "5.0.0"
app_file: app.py
pinned: false
---

# audio_pull

Bulk YouTube audio downloader. Downloads audio from one or many YouTube URLs, names each file after the video title, and saves everything to a local `downloads/` folder. Supports playlists, concurrent downloads, and built-in duplicate detection.

Includes a `--ringtone` mode that exports iPhone-ready `.m4r` clips.

---

## Requirements

| Dependency | Purpose |
|---|---|
| Python 3.10+ | Runtime |
| `yt-dlp` | YouTube extraction engine |
| `tqdm` | Progress bars |
| `pathvalidate` | Cross-platform filename sanitization |
| **ffmpeg** (system binary) | Audio conversion & ringtone export |

### Install ffmpeg

- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH, or via `winget install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg` / `sudo dnf install ffmpeg`

---

## Setup

```bash
# 1. Clone / navigate to the project
cd audio_pull

# 2. (Optional but recommended) create a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows

# 3. Install Python dependencies
pip install -r requirements.txt
```

---

## Usage

### Download a single video

```bash
python audio_pull.py https://youtu.be/dQw4w9WgXcQ
```

### Download multiple videos at once

```bash
python audio_pull.py https://youtu.be/XXX https://youtu.be/YYY https://youtu.be/ZZZ
```

### Download from a text file

Create a `urls.txt` file with one URL per line:

```
# My playlist
https://youtu.be/XXX
https://youtu.be/YYY

# Lines starting with # are ignored
https://youtu.be/ZZZ
```

```bash
python audio_pull.py --file urls.txt
```

### Download a full YouTube playlist

```bash
python audio_pull.py "https://www.youtube.com/playlist?list=PLXXXXXXXXXX"
```

### Choose audio format

```bash
# MP3 (maximum compatibility)
python audio_pull.py --format mp3 https://youtu.be/XXX

# Opus (best quality at low bitrates)
python audio_pull.py --format opus https://youtu.be/XXX

# M4A (default - YouTube's native codec, no re-encoding loss)
python audio_pull.py --format m4a https://youtu.be/XXX
```

### Speed up with more parallel workers

```bash
python audio_pull.py --workers 5 --file urls.txt
```

---

## iPhone Ringtone Mode

The `--ringtone` flag downloads the audio **and** exports a trimmed `.m4r` file to `downloads/ringtones/`. The `.m4r` format is what iOS uses for ringtones - it's just AAC audio in an MP4 container.

### Export a ringtone (first 30 seconds)

```bash
python audio_pull.py --ringtone https://youtu.be/XXX
```

### Choose which part of the song to use

Use `--start` (timestamp) and `--duration` (seconds) to clip the best part - usually the chorus or hook:

```bash
# Start at 1 minute, use 30 seconds
python audio_pull.py --ringtone --start 00:01:00 --duration 30 https://youtu.be/XXX

# Start at 45 seconds, use 25 seconds  
python audio_pull.py --ringtone --start 00:00:45 --duration 25 https://youtu.be/XXX
```

> **Limits:** iPhone ringtones max out at **40 seconds**. Alert tones and alarm tones max out at **30 seconds**.

### Export 3 ringtones per track - intro, mid, outro

Use `--ringtone-all` to automatically generate three clips from each track:

| Clip | Starts at | Label in filename |
|---|---|---|
| Intro | 0:00 | `[intro]` |
| Mid | 0:45 | `[mid]` |
| Outro | 1:30 | `[outro]` |

The gap pattern is: **30s clip → skip 15s → 30s clip → skip 15s → 30s clip**.

```bash
python audio_pull.py --ringtone-all https://youtu.be/XXX
```

Output in `downloads/ringtones/`:
```
Song Title [intro].m4r
Song Title [mid].m4r
Song Title [outro].m4r
```

Also works with `--ringtone-only` for already-downloaded files:

```bash
python audio_pull.py --ringtone-only "Song Title.m4a" --ringtone-all
```

### Already downloaded a track and want to make it a ringtone?

Use `--ringtone-only` to convert any existing file in `downloads/` - no re-downloading:

```bash
# Filename only - the script looks in downloads/ automatically
python audio_pull.py --ringtone-only "Song Title.m4a"

# With a custom clip (start at 45 seconds, 30 seconds long)
python audio_pull.py --ringtone-only "Song Title.m4a" --start 00:00:45 --duration 30

# Or pass a full path to a file anywhere on your system
python audio_pull.py --ringtone-only "/path/to/any/audio.m4a" --start 00:01:00
```

The `.m4r` is saved to `downloads/ringtones/` as usual.

### Install the ringtone on your iPhone

**Easiest - AirDrop (iOS 14+):**

1. AirDrop the `.m4r` file from your Mac or PC to your iPhone
2. On iPhone, tap **Accept**
3. Go to **Settings → Sounds & Haptics → Ringtone**
4. Your new ringtone appears at the top of the list

**Via iTunes (Windows):**

1. Open iTunes and connect your iPhone via USB
2. Drag the `.m4r` file into the **Tones** section in the iTunes sidebar
3. Sync your iPhone

**Via Finder (Mac, macOS Catalina+):**

1. Double-click the `.m4r` file - it imports into the Music app automatically
2. Connect your iPhone and sync Tones

---

## All Options

```
usage: audio_pull [-h] [--file FILE] [--format {m4a,mp3,opus}] [--workers N]
                  [--no-metadata] [--ringtone] [--start HH:MM:SS]
                  [--duration SECONDS] [--verbose]
                  [URL ...]

positional arguments:
  URL                       One or more YouTube URLs (videos or playlists)

options:
  -h, --help                Show this help message and exit
  --file FILE, -f FILE      Text file with one URL per line
  --format {m4a,mp3,opus}   Output audio format (default: m4a)
  --workers N, -w N         Max concurrent downloads (default: 3)
  --no-metadata             Skip embedding metadata tags into audio files
  --ringtone                Export a single .m4r ringtone (uses --start and --duration)
  --ringtone-all            Export 3 ringtones per track: [intro], [mid], [outro]
  --ringtone-only FILE      Convert an already-downloaded file to .m4r (no re-download)
  --start HH:MM:SS          Ringtone clip start time (default: 00:00:00)
  --duration SECONDS        Ringtone clip length in seconds, max 40 (default: 30)
  --verbose, -v             Enable debug-level logging
```

---

## Output

```
audio_pull/
├── audio_pull.py
├── requirements.txt
├── README.md
├── .gitignore
└── downloads/               ← gitignored
    ├── .yt_archive          ← tracks downloaded video IDs (prevents re-downloads)
    ├── Song Title Here.m4a
    ├── Another Track.m4a
    └── ringtones/           ← only created when --ringtone is used
        ├── Song Title Here.m4r
        └── Another Track.m4r
```

The `.yt_archive` file means re-running the same URL list will skip already-downloaded videos automatically.

---

## Format Comparison

| Format | Quality | File Size | Compatibility | Notes |
|---|---|---|---|---|
| **M4A** (default) | Best | Medium | Excellent | YouTube's native codec - no re-encoding |
| **MP3** | Good | Medium | Universal | Requires transcoding; best for old devices |
| **Opus** | Excellent at low bitrates | Small | Modern apps | Best if file size matters |
