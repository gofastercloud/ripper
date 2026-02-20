# ripper

**Rip, compress & organize your physical media collection from the couch.**

Pop a disc in, run one command, and ripper handles the rest — ripping with [MakeMKV](https://www.makemkv.com/), compressing with [HandBrake](https://handbrake.fr/) (HDR10 and Dolby Vision preserved), fetching metadata from [TMDb](https://www.themoviedb.org/), and dropping perfectly named files into your [Jellyfin](https://jellyfin.org/) or [Plex](https://www.plex.tv/) library.

```
$ uv run ripper
```

That's it. First run walks you through setup.

## What it does

- Auto-detects disc type (movie vs TV) and source format (DVD / Blu-ray / 4K UHD)
- Fetches metadata, posters, and fanart from TMDb
- Hardware-accelerated encoding via VideoToolbox (or software x265 with `--hq`)
- Auto-optimized encoding per source type (DVD / Blu-ray / UHD) with viewing profiles
- TV discs encode in parallel — episode 1 encodes while episode 2 rips
- Per-episode Jellyfin library scans (episodes appear as they finish)
- Preserved raw rips so you can re-encode later without re-ripping
- Graceful Ctrl+C — won't leave your library in a weird state
- Rich TUI with live progress bars, episode queue, and scrolling log

## Prerequisites

macOS with Homebrew:

```bash
brew install uv
brew install --cask makemkv
brew install handbrake
brew install ffmpeg
```

| Tool | What it does | Link |
|------|-------------|------|
| [MakeMKV](https://www.makemkv.com/) | Rips disc to lossless MKV | [makemkv.com](https://www.makemkv.com/) |
| [HandBrake](https://handbrake.fr/) | Compresses video (H.265, VideoToolbox) | [handbrake.fr](https://handbrake.fr/) |
| [Jellyfin](https://jellyfin.org/) | Media server (library scan integration) | [jellyfin.org](https://jellyfin.org/) |
| [TMDb](https://www.themoviedb.org/) | Metadata, posters, fanart | [themoviedb.org](https://www.themoviedb.org/) |
| [ffmpeg](https://ffmpeg.org/) | Post-encode quality analysis (optional) | [ffmpeg.org](https://ffmpeg.org/) |
| [uv](https://docs.astral.sh/uv/) | Runs the script (manages Python + deps) | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |

You'll need a free [TMDb API key](https://www.themoviedb.org/settings/api). Jellyfin integration is optional but recommended.

An optical drive helps too. Obviously.

## Quick start

```bash
# Clone and run — no virtualenv, no pip install, nothing
git clone https://github.com/gofastercloud/ripper.git
cd ripper
uv run ripper
```

`uv` handles everything — the right Python version, dependencies (`rich`, `Pillow`), and execution. No virtualenv, no `pip install`, no setup steps. Just `uv run ripper`.

Or install it globally so `ripper` works from anywhere:

```bash
uv tool install .
ripper --status
```

The first run will ask you a few questions (where to put files, API keys, encoder preference) and save everything to `~/.config/ripper/config.json`.

## Usage

```bash
# Auto-detect everything from the disc
uv run ripper

# Specify a title (skips TMDb search guessing)
uv run ripper --title "The Matrix" --year 1999

# TV show — season 2, starting at episode 1
uv run ripper --tv --season 2

# TV show — disc has episodes 5-8
uv run ripper --tv --season 1 --episode 5

# Best quality (software x265, slower)
uv run ripper --hq

# Grainy/filmic source (implies --hq)
uv run ripper --grain

# Just rip, don't encode
uv run ripper --rip-only

# Just encode an existing MKV
uv run ripper --compress-only /path/to/file.mkv --title "Movie Name"

# Re-encode from preserved rips
uv run ripper --reencode _rips/The_Matrix_1999/

# Watch mode — auto-rips when you insert a disc
uv run ripper --watch

# Check everything is working
uv run ripper --status

# Trigger a Jellyfin library scan
uv run ripper --scan

# Free up space by deleting old rips
uv run ripper --cleanup

# Repair library metadata (re-fetch missing NFOs, posters)
uv run ripper --repair

# Reconfigure
uv run ripper --init
```

## Output structure

```
<media_root>/
  Movies/
    The Matrix (1999)/
      The Matrix (1999).mkv
      movie.nfo
      poster.jpg
      fanart.jpg
  Shows/
    Breaking Bad (2008)/
      tvshow.nfo
      Season 01/
        Breaking Bad - S01E01 - Pilot.mkv
  _rips/          preserved raw rips (--cleanup to remove)
  _cache/         TMDb metadata cache
  _logs/          pipeline logs
```

## Configuration

Settings live at `~/.config/ripper/config.json`. You can:

- Run `--init` to go through the setup wizard again
- Run `--show-config` to see current settings
- Edit the JSON directly
- Set `TMDB_API_KEY` and `JELLYFIN_API_KEY` as environment variables (overrides config)
- Set `audio_lang` to control which audio languages are included (default: `"eng,und"` — English and untagged tracks)

## Encoding

ripper auto-optimizes encoding settings based on your source disc and viewing setup. The setup wizard (`--init`) asks for your primary display, which sets per-source quality targets:

| Profile | UHD RF | BD RF | DVD RF | Audio | Best for |
|---------|--------|-------|--------|-------|----------|
| OLED TV | 48 | 50 | 58 | Lossless + AAC | Home theater |
| LED TV | 50 | 52 | 60 | Lossless + AAC | Living room |
| Tablet | 55 | 55 | 62 | AAC only | Portable |
| Phone | 58 | 58 | 65 | AAC only | On the go |
| Mixed | 50 | 52 | 60 | Lossless + AAC | Multiple devices |

All encoding uses VideoToolbox hardware acceleration. DVDs automatically get deinterlacing. UHD discs preserve HDR10 and Dolby Vision.

You can override per-source RF values in `~/.config/ripper/config.json` (`quality_rf_dvd`, `quality_rf_bd`, `quality_rf_uhd`) or use `--quality` for a one-off override. Use `--hq` to force software x265 encoding (slower, maximum quality).

## How it works

It's a small Python package — a handful of focused modules, no framework, no plugins, no microservices.

1. **Detect** — reads the disc label, figures out if it's a movie or TV show
2. **Metadata** — searches TMDb, lets you pick if it's ambiguous, caches results
3. **Rip** — [MakeMKV](https://www.makemkv.com/) pulls the main title(s) off the disc
4. **Compress** — [HandBrake](https://handbrake.fr/) encodes with format-appropriate settings
5. **Organize** — moves files into [Jellyfin](https://jellyfin.org/)-compatible folder structure with NFO metadata
6. **Scan** — pokes Jellyfin to pick up the new content

For TV discs, steps 3-6 run in a pipeline — encoding one episode while ripping the next.

## Development

```bash
uv run pytest          # Run tests
uv run ruff check .    # Lint
```

## License

[MIT](LICENSE) — do whatever you want with it.
