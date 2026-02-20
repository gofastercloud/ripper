# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ripper is a Python CLI tool that automates ripping physical media (Blu-ray 4K UHD, 1080p, DVD) using MakeMKV, compressing with HandBrake (HDR10/Dolby Vision preservation), and organizing output with Jellyfin/Plex-compatible naming and NFO metadata from TMDb.

## Commands

```bash
uv run ripper                           # Auto-detect and rip inserted disc
uv run ripper --title "The Matrix"      # Specific title
uv run ripper --tv --season 2           # TV disc
uv run ripper --hq                      # Software x265 (slower, best quality)
uv run ripper --status                  # Check drive, Jellyfin, API keys
uv run ripper --init                    # Reconfigure settings
uv run ripper --show-config             # Print config
uv run ripper --repair                  # Re-fetch missing NFOs/posters

uv run pytest                           # Run all tests
uv run pytest tests/test_media.py -v    # Single test file
uv run ruff check .                     # Lint
uv run ruff check --fix .               # Auto-fix lint
```

## Architecture

Python package under `ripper/` with these modules:

| Module | Responsibility |
|--------|---------------|
| `state.py` | Shared mutable globals: `CONFIG`, `tui`, `log`, `_shutdown_requested`, `_active_processes`. Imports nothing from ripper (no circular deps). |
| `config.py` | `CONFIG` defaults, `_VIEWING_PROFILES`, config load/save, setup wizard. Imports: state |
| `helpers.py` | `run_cmd`, `run_cmd_progress`, `sanitize_filename`, `eject_disc`, filesystem utils. Imports: state |
| `media.py` | `detect_source_format`, `auto_tune_for_source`, MakeMKV/HandBrake progress parsers. Imports: state |
| `tui.py` | `RipperTUI` (Rich live UI), `_TUILogHandler`, `poster_to_ascii`, `setup_logging`. Imports: state |
| `metadata.py` | TMDb API (search, fetch, cache), disc label parsing, NFO XML, artwork download. Imports: state, helpers |
| `jellyfin.py` | `jellyfin_scan_library`, `jellyfin_check_status`. Imports: state |
| `cleanup.py` | Rip manifests, `cleanup_rips`, `repair_library`. Imports: state, helpers, metadata |
| `pipeline.py` | Main orchestration: `rip_disc`, `compress_mkv`, `organize_file`, `run_pipeline`, `watch_mode`. Imports: all modules |
| `cli.py` | Entry point `main()`, argparse, signal handling. Imports: all modules |

## Key Patterns

- **Shared state**: All globals live in `state.py`. Modules access them as `state.CONFIG`, `state.tui`, etc.
- **Shutdown**: `state.request_shutdown()` / `state.is_shutdown_requested()` checked at loop boundaries
- **External tools**: Requires `makemkvcon`, `HandBrakeCLI`, `drutil`, `diskutil` (macOS)
- **Threading**: TV parallel pipeline uses `threading.Thread` + `queue.Queue` for concurrent rip/encode
- **TMDb API**: Uses `urllib.request` directly (no `requests` library), results cached to disk
- **No circular imports**: Module dependency graph is strictly layered (state ← leaf modules ← domain modules ← pipeline ← cli)

## Output Structure

```
<media_root>/
  Movies/<Title> (<Year>)/<Title> (<Year>).mkv + movie.nfo + poster.jpg
  Shows/<Title> (<Year>)/Season XX/<Title> - S01E01 - Episode.mkv
  _rips/      (preserved raw MKV rips)
  _cache/     (TMDb metadata cache)
  _logs/      (pipeline logs)
```
