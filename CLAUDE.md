# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ripper is a single-file Python CLI tool (`ripper.py`, ~3500 lines) that automates ripping physical media (Blu-ray 4K UHD, 1080p, DVD) using MakeMKV, compressing with HandBrake (HDR10/Dolby Vision preservation), and organizing output with Jellyfin/Plex-compatible naming and NFO metadata from TMDb.

## Running

```bash
uv run ripper.py                        # Auto-detect and rip inserted disc
uv run ripper.py --title "The Matrix"   # Specific title
uv run ripper.py --tv --season 2        # TV disc
uv run ripper.py --hq                   # Software x265 (slower, best quality)
uv run ripper.py --status               # Check drive, Jellyfin, API keys
uv run ripper.py --init                 # Reconfigure settings
uv run ripper.py --show-config          # Print config
```

Uses `uv` inline script metadata (PEP 723) — no virtualenv or `pip install` needed. The only runtime Python dependency is `rich`.

## Architecture

Single-file monolith with these logical sections (top to bottom):

1. **TUI** (`RipperTUI` class, line ~94) — Rich-based live terminal UI with progress bars, episode queue, and scrolling log
2. **Signal handling** (`_signal_handler`, line ~319) — Graceful Ctrl+C shutdown with cleanup
3. **Configuration** (line ~405) — Config at `~/.config/ripper/config.json`, first-run setup wizard, env var overrides for `TMDB_API_KEY`/`JELLYFIN_API_KEY`
4. **Logging** (`setup_logging`, line ~610) — Dual output: file log + stdout (suppressed when TUI active via `_TUILogHandler`)
5. **Drive detection** (`check_drive`, line ~641) — macOS `diskutil`/`drutil` for optical drive and disc detection
6. **Subprocess runners** (`run_cmd`, `run_cmd_progress`, line ~722) — Command execution with real-time progress parsing
7. **MakeMKV integration** (line ~799) — Progress parsing, disc scanning, ripping
8. **HandBrake integration** (line ~832) — Progress parsing, encoding with VideoToolbox (default) or software x265
9. **Source format detection** (`detect_source_format`, `auto_tune_for_source`, line ~859) — Auto-detects DVD/BD/UHD from MakeMKV scan output and tunes encoder settings accordingly (DVD gets deinterlace + software x265)
10. **TMDb API** (line ~1228) — Movie/TV search, metadata fetch, season episodes, caching at `_cache/`
11. **NFO/artwork** (line ~1891) — Jellyfin-compatible NFO XML generation and poster/fanart download
12. **Pipeline** (`run_pipeline`, `_run_pipeline_inner`, line ~2840) — Main orchestration: detect → metadata → rip → compress → organize → scan. TV mode uses parallel rip+encode (encode ep N while ripping ep N+1)
13. **CLI** (`main`, line ~3127) — argparse with pre-parsing for `--init`/`--config`/`--show-config`

## Key Patterns

- **No tests or linting configured** — single-file script, no test framework
- **External tools**: Requires `makemkvcon`, `HandBrakeCLI`, `drutil`, `diskutil` (macOS)
- **Threading**: TV parallel pipeline uses `threading.Thread` + `queue.Queue` for concurrent rip/encode
- **TMDb API**: Uses `urllib.request` directly (no `requests` library), results cached to disk
- **Config is a global `CONFIG` dict** modified in-place throughout the codebase
- **`_shutdown_requested` global** checked at loop boundaries for graceful interrupt

## Output Structure

```
<media_root>/
  Movies/<Title> (<Year>)/<Title> (<Year>).mkv + movie.nfo + poster.jpg
  Shows/<Title> (<Year>)/Season XX/<Title> - S01E01 - Episode.mkv
  _rips/      (preserved raw MKV rips)
  _cache/     (TMDb metadata cache)
  _logs/      (pipeline logs)
```
