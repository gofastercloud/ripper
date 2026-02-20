#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "rich>=13.0",
#     "Pillow>=10.0",
# ]
# ///
"""
ripper — Rip, Compress & Organize Physical Media
==================================================

Automated pipeline for ripping Blu-ray (4K UHD, 1080p) and DVD discs using
MakeMKV, compressing with HandBrake (preserving HDR10/Dolby Vision), and
organizing output with Jellyfin/Plex-compatible naming and metadata.

Features:
  - Auto-detects disc type (movie vs TV) and source format (DVD/BD/UHD)
  - TMDb metadata lookup with interactive fallback for unrecognised discs
  - Auto-optimized encoding per source (DVD/BD/UHD) with viewing profiles
  - VideoToolbox hardware encoding, software x265 available with --hq
  - Parallel rip+encode for TV discs (encode ep1 while ripping ep2)
  - Per-episode Jellyfin library scan (episodes appear as they complete)
  - Rip integrity verification with MD5 manifests
  - Preserved rips for re-encoding without re-ripping
  - Graceful Ctrl+C handling

Prerequisites:
  brew install uv
  brew install --cask makemkv
  brew install handbrake

Quick start:
  1. Get a free TMDb API key: https://www.themoviedb.org/settings/api
  2. Run:  uv run ripper.py
     (first run will walk you through setup)

Configuration:
  Settings are stored in ~/.config/ripper/config.json
  Run --init to reconfigure, --show-config to view, or edit directly.
  Environment variables TMDB_API_KEY and JELLYFIN_API_KEY override the config.

Output structure:
  <media_root>/
    Movies/
      Movie Name (2024)/
        Movie Name (2024).mkv
        movie.nfo
        poster.jpg / fanart.jpg
    Shows/
      Show Name (2020)/
        tvshow.nfo
        Season 01/
          Show Name - S01E01 - Episode Title.mkv
    _rips/          (preserved raw rips, use --cleanup to remove)
    _cache/         (TMDb metadata cache)
    _logs/          (pipeline logs)
"""

import subprocess
import sys
import os
import re
import json
import shutil
import argparse
import time
import signal
import logging
import urllib.request
import urllib.parse
import urllib.error
import threading
import queue
import hashlib
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
from rich.layout import Layout
from rich.text import Text
from rich import box

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

console = Console()


# ============================================================================
# POSTER ASCII ART
# ============================================================================

_poster_cache = {}  # keyed by poster_path


def poster_to_ascii(poster_path, width=20, height=40):
    """
    Download a TMDb poster thumbnail and convert to Rich Text using half-block
    characters (▀) for double vertical resolution with true-color output.

    Returns a Rich Text renderable, or None on failure.
    """
    if not poster_path or PILImage is None:
        return None

    if poster_path in _poster_cache:
        return _poster_cache[poster_path]

    try:
        url = f"https://image.tmdb.org/t/p/w185{poster_path}"
        req = urllib.request.Request(url, headers={"User-Agent": "ripper/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            img_data = resp.read()

        import io
        img = PILImage.open(io.BytesIO(img_data)).convert("RGB")

        # Resize: each character cell is ~width chars, height rows
        # Using half-block ▀ means 2 pixel rows per terminal row
        img = img.resize((width, height), PILImage.LANCZOS)

        pixels = img.load()
        lines = []
        for row in range(0, height - 1, 2):
            line_parts = []
            for col in range(width):
                top_r, top_g, top_b = pixels[col, row]
                bot_r, bot_g, bot_b = pixels[col, row + 1]
                # Upper half-block: foreground = top pixel, background = bottom pixel
                line_parts.append(
                    f"[rgb({top_r},{top_g},{top_b}) on rgb({bot_r},{bot_g},{bot_b})]▀[/]"
                )
            lines.append("".join(line_parts))

        result = Text.from_markup("\n".join(lines))
        _poster_cache[poster_path] = result
        return result

    except Exception:
        _poster_cache[poster_path] = None
        return None


# ============================================================================
# TUI — Rich-based terminal UI
# ============================================================================

class RipperTUI:
    """
    Live terminal UI using rich. Shows:
      - Header with title/metadata
      - Rip progress bar
      - Encode progress bar
      - Episode queue with status indicators
      - Scrolling log messages
    """

    def __init__(self):
        self.live = None
        self.enabled = False

        # State
        self.title = ""
        self.year = ""
        self.media_type = "movie"
        self.source_format = ""
        self.encoder_mode = ""

        # Progress
        self.rip_pct = 0.0
        self.rip_task = ""       # Current rip task description
        self.encode_pct = 0.0
        self.encode_eta = ""
        self.encode_task = ""    # Current encode task description

        # Episode queue (TV only)
        self.episodes = []       # List of {"num": int, "name": str, "status": str}
        self.current_rip_ep = None
        self.current_encode_ep = None

        # Poster art (Rich renderable or None)
        self.poster_art = None

        # Log messages (rolling buffer)
        self._log_lines = []
        self._max_log = 8
        self._lock = threading.Lock()

    def start(self):
        """Start the live display and suppress the stdout log handler."""
        self.enabled = True
        # Suppress stdout StreamHandler — TUI handles log display now
        if _stdout_handler and log:
            log.root.removeHandler(_stdout_handler)
        self.live = Live(
            self._render(),
            console=console,
            refresh_per_second=4,
            transient=False,
        )
        self.live.start()

    def stop(self):
        """Stop the live display and restore the stdout log handler."""
        if self.live:
            self.live.stop()
            self.live = None
        self.enabled = False
        # Restore stdout StreamHandler
        if _stdout_handler and log:
            log.root.addHandler(_stdout_handler)

    def log(self, msg):
        """Add a message to the scrolling log area."""
        with self._lock:
            self._log_lines.append(msg)
            if len(self._log_lines) > self._max_log:
                self._log_lines = self._log_lines[-self._max_log:]
        self._refresh()

    def set_metadata(self, title, year=None, media_type="movie",
                     source_format="", encoder_mode=""):
        """Set the header metadata."""
        self.title = title
        self.year = year or ""
        self.media_type = media_type
        self.source_format = source_format
        self.encoder_mode = encoder_mode
        self._refresh()

    def set_poster(self, poster_renderable):
        """Set the poster art renderable for the TUI."""
        self.poster_art = poster_renderable
        self._refresh()

    def set_episodes(self, episodes):
        """
        Set the episode queue. episodes: list of (ep_num, ep_name) tuples.
        All start as 'pending'.
        """
        self.episodes = [
            {"num": num, "name": name, "status": "pending"}
            for num, name in episodes
        ]
        self._refresh()

    def set_episode_status(self, ep_num, status):
        """Update episode status: pending / ripping / encoding / done / failed."""
        for ep in self.episodes:
            if ep["num"] == ep_num:
                ep["status"] = status
                break
        self._refresh()

    def update_rip(self, pct, task=""):
        """Update rip progress."""
        self.rip_pct = pct
        if task:
            self.rip_task = task
        self._refresh()

    def update_encode(self, pct, eta="", task=""):
        """Update encode progress."""
        self.encode_pct = pct
        self.encode_eta = eta
        if task:
            self.encode_task = task
        self._refresh()

    def _refresh(self):
        """Push a new render to the live display."""
        if self.live and self.enabled:
            try:
                self.live.update(self._render())
            except Exception:
                pass  # Don't crash on render errors

    def _render(self):
        """Build the full layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="log", size=min(self._max_log + 2, 10)),
        )

        # --- Header ---
        year_str = f" ({self.year})" if self.year else ""
        type_badge = f"[bold cyan]{self.media_type.upper()}[/]"
        source_badge = f"[dim]{self.source_format}[/]" if self.source_format else ""
        enc_badge = f"[dim]{self.encoder_mode}[/]" if self.encoder_mode else ""
        header_text = f" {type_badge}  [bold white]{self.title}{year_str}[/]  {source_badge}  {enc_badge}"
        layout["header"].update(Panel(header_text, box=box.HEAVY, style="blue"))

        # --- Body: depends on movie vs TV, with optional poster ---
        if self.poster_art:
            poster_panel = Panel(
                self.poster_art, title="[dim]Poster[/]",
                box=box.ROUNDED, style="dim",
            )
            if self.episodes:
                # TV mode: poster | progress | queue
                layout["body"].split_row(
                    Layout(name="poster", ratio=1),
                    Layout(name="progress", ratio=1),
                    Layout(name="queue", ratio=1),
                )
                layout["body"]["poster"].update(poster_panel)
                layout["body"]["progress"].update(self._render_progress())
                layout["body"]["queue"].update(self._render_queue())
            else:
                # Movie mode: poster | progress
                layout["body"].split_row(
                    Layout(name="poster", ratio=1),
                    Layout(name="progress", ratio=2),
                )
                layout["body"]["poster"].update(poster_panel)
                layout["body"]["progress"].update(self._render_progress())
        elif self.episodes:
            # TV mode without poster: progress | queue
            layout["body"].split_row(
                Layout(name="progress", ratio=1),
                Layout(name="queue", ratio=1),
            )
            layout["body"]["progress"].update(self._render_progress())
            layout["body"]["queue"].update(self._render_queue())
        else:
            # Movie mode without poster: just progress bars
            layout["body"].update(self._render_progress())

        # --- Log ---
        with self._lock:
            log_text = "\n".join(self._log_lines[-self._max_log:]) if self._log_lines else "[dim]Waiting...[/]"
        layout["log"].update(Panel(log_text, title="[dim]Log[/]", box=box.ROUNDED, style="dim"))

        return layout

    def _render_progress(self):
        """Render the rip and encode progress bars."""
        table = Table(box=None, show_header=False, expand=True, padding=(1, 2))
        table.add_column(ratio=1)

        # Rip progress
        rip_bar = self._bar_string(self.rip_pct, "green")
        rip_label = self.rip_task or "Rip"
        table.add_row(f"[bold]RIP[/]  {rip_label}")
        table.add_row(f"  {rip_bar}  [bold]{self.rip_pct:5.1f}%[/]")
        table.add_row("")

        # Encode progress
        enc_bar = self._bar_string(self.encode_pct, "yellow")
        enc_label = self.encode_task or "Encode"
        eta_str = f"  ETA {self.encode_eta}" if self.encode_eta else ""
        table.add_row(f"[bold]ENC[/]  {enc_label}")
        table.add_row(f"  {enc_bar}  [bold]{self.encode_pct:5.1f}%[/]{eta_str}")

        return Panel(table, title="[bold]Progress[/]", box=box.ROUNDED)

    def _render_queue(self):
        """Render the episode queue."""
        table = Table(box=box.SIMPLE, expand=True, show_header=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Episode", ratio=1)
        table.add_column("Status", width=10, justify="right")

        status_styles = {
            "pending":  "[dim]waiting[/]",
            "ripping":  "[bold green]ripping[/]",
            "ripped":   "[green]ripped[/]",
            "encoding": "[bold yellow]encoding[/]",
            "done":     "[bold blue]done[/]",
            "failed":   "[bold red]FAILED[/]",
        }

        for ep in self.episodes:
            num_str = f"E{ep['num']:02d}"
            name = ep["name"] or f"Episode {ep['num']}"
            status = status_styles.get(ep["status"], ep["status"])
            table.add_row(num_str, name, status)

        return Panel(table, title="[bold]Episodes[/]", box=box.ROUNDED)

    @staticmethod
    def _bar_string(pct, color, width=30):
        """Render an ASCII progress bar."""
        filled = int(width * pct / 100)
        empty = width - filled
        return f"[{color}]{'━' * filled}[/][dim]{'─' * empty}[/]"


# Global TUI instance (None when TUI is disabled)
tui = None  # type: RipperTUI | None


# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================

_shutdown_requested = False
_active_processes = []  # Track subprocesses for cleanup (thread-safe via GIL)
_active_processes_lock = threading.Lock()


def _signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    global _shutdown_requested
    if _shutdown_requested:
        # Second Ctrl+C — force exit
        print("\n\nForce quit. Intermediate files may remain in _rips/.")
        sys.exit(1)
    _shutdown_requested = True
    print("\n\nShutting down gracefully (Ctrl+C again to force quit)...")
    print("Waiting for current operation(s) to finish...")
    with _active_processes_lock:
        for proc in _active_processes:
            try:
                proc.terminate()
            except OSError:
                pass


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ============================================================================
# CONFIGURATION
# ============================================================================
#
# No hardcoded paths. All settings come from:
#   1. Config file (~/.config/ripper/config.json) — created by --init or first run
#   2. Environment variables (TMDB_API_KEY, JELLYFIN_API_KEY) override config
#   3. CLI flags override everything
#
# Defaults below are sensible starting points; output_base is None to force
# setup on first run.

CONFIG = {
    # Output paths — None means "not configured, run setup"
    "output_base": None,
    "rip_dir": None,
    "encode_dir": None,
    "tv_encode_dir": None,
    "log_dir": None,
    "metadata_cache": None,

    # MakeMKV
    "makemkv_bin": None,            # Auto-detected in setup
    "min_title_length": 1200,       # Skip titles < 20 min (movies)
    "min_episode_length": 600,      # Skip titles < 10 min (TV episodes)

    # HandBrake — VideoToolbox hardware encoding by default
    "handbrake_bin": None,          # Auto-detected in setup
    "encoder": "vt_h265_10bit",
    "quality_rf": 55,               # VT scale (50-70, lower = better)
    "encoder_preset": "quality",    # VT preset: speed / balanced / quality
    "encoder_tune": None,           # "grain" for filmic sources (x265 only)
    "encoder_profile": "main10",    # Required for HDR10
    "encoder_level": "auto",
    "hq_mode": False,               # --hq flag switches to software x265
    "deinterlace": False,           # Auto-enabled for DVDs
    "output_format": "mkv",

    # Auto-optimized encoding — set by viewing profile
    "viewing_profile": "mixed",
    "quality_rf_dvd": 60,
    "quality_rf_bd": 52,
    "quality_rf_uhd": 50,
    "audio_mode": "copy,aac",       # "copy,aac" or "aac"

    # Polling interval for --watch mode (seconds)
    "poll_interval": 10,

    # TMDb API (free key from https://www.themoviedb.org/settings/api)
    "tmdb_api_key": "",

    # Jellyfin integration
    "jellyfin_url": "http://localhost:8096",
    "jellyfin_api_key": "",
}

# Default config file location
CONFIG_PATH = Path.home() / ".config" / "ripper" / "config.json"

# Viewing profiles: per-source RF and audio settings
_VIEWING_PROFILES = {
    "oled":   {"quality_rf_uhd": 48, "quality_rf_bd": 50, "quality_rf_dvd": 58, "audio_mode": "copy,aac"},
    "led":    {"quality_rf_uhd": 50, "quality_rf_bd": 52, "quality_rf_dvd": 60, "audio_mode": "copy,aac"},
    "tablet": {"quality_rf_uhd": 55, "quality_rf_bd": 55, "quality_rf_dvd": 62, "audio_mode": "aac"},
    "phone":  {"quality_rf_uhd": 58, "quality_rf_bd": 58, "quality_rf_dvd": 65, "audio_mode": "aac"},
    "mixed":  {"quality_rf_uhd": 50, "quality_rf_bd": 52, "quality_rf_dvd": 60, "audio_mode": "copy,aac"},
}

# Keys that are safe/useful to persist in config file
_CONFIGURABLE_KEYS = {
    "output_base", "rip_dir", "encode_dir", "tv_encode_dir", "log_dir",
    "makemkv_bin", "handbrake_bin",
    "min_title_length", "min_episode_length",
    "encoder", "quality_rf", "encoder_preset", "encoder_tune",
    "encoder_profile", "encoder_level", "hq_mode", "output_format",
    "viewing_profile", "quality_rf_dvd", "quality_rf_bd", "quality_rf_uhd",
    "audio_mode",
    "poll_interval",
    "tmdb_api_key", "metadata_cache",
    "jellyfin_url", "jellyfin_api_key",
}


def _find_binary(name, mac_app_path=None):
    """Try to find a binary: check PATH first, then common macOS locations."""
    # Check PATH
    try:
        result = subprocess.run(
            ["which", name], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Check macOS .app bundle
    if mac_app_path and Path(mac_app_path).exists():
        return mac_app_path
    return None


def _derive_paths(base):
    """Set all dependent paths from a media root directory."""
    CONFIG["output_base"] = base
    CONFIG["rip_dir"] = f"{base}/_rips"
    CONFIG["encode_dir"] = f"{base}/Movies"
    CONFIG["tv_encode_dir"] = f"{base}/Shows"
    CONFIG["log_dir"] = f"{base}/_logs"
    CONFIG["metadata_cache"] = f"{base}/_cache/metadata.json"


def _detect_defaults():
    """Auto-detect sensible defaults for the current platform."""
    # MakeMKV
    if not CONFIG.get("makemkv_bin"):
        found = _find_binary(
            "makemkvcon",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
        )
        if found:
            CONFIG["makemkv_bin"] = found

    # HandBrake
    if not CONFIG.get("handbrake_bin"):
        found = _find_binary("HandBrakeCLI")
        if found:
            CONFIG["handbrake_bin"] = found
        else:
            CONFIG["handbrake_bin"] = "HandBrakeCLI"  # Hope it's on PATH

    # Default media root: ~/Media if nothing configured
    if not CONFIG.get("output_base"):
        CONFIG["output_base"] = str(Path.home() / "Media")
        _derive_paths(CONFIG["output_base"])


def is_configured():
    """Check if a config file exists and has been set up."""
    return CONFIG_PATH.exists()


def load_config(config_path=None):
    """
    Load config from file, merging with defaults.
    Priority: CLI flags > environment variables > config file > auto-detected defaults.
    """
    path = Path(config_path) if config_path else CONFIG_PATH

    if path.exists():
        try:
            with open(path) as f:
                user_config = json.load(f)
            for key, value in user_config.items():
                if key in _CONFIGURABLE_KEYS and value is not None:
                    CONFIG[key] = value
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Could not load config from {path}: {e}")

    # Environment variables always override config file
    if os.environ.get("TMDB_API_KEY"):
        CONFIG["tmdb_api_key"] = os.environ["TMDB_API_KEY"]
    if os.environ.get("JELLYFIN_API_KEY"):
        CONFIG["jellyfin_api_key"] = os.environ["JELLYFIN_API_KEY"]

    # Derive dependent paths from output_base if not individually set
    base = CONFIG.get("output_base")
    if base:
        if not CONFIG.get("rip_dir"):
            CONFIG["rip_dir"] = f"{base}/_rips"
        if not CONFIG.get("encode_dir"):
            CONFIG["encode_dir"] = f"{base}/Movies"
        if not CONFIG.get("tv_encode_dir"):
            CONFIG["tv_encode_dir"] = f"{base}/Shows"
        if not CONFIG.get("log_dir"):
            CONFIG["log_dir"] = f"{base}/_logs"
        if not CONFIG.get("metadata_cache"):
            CONFIG["metadata_cache"] = f"{base}/_cache/metadata.json"

    # Fill in anything still missing with auto-detection
    _detect_defaults()


def save_config(config_path=None):
    """Save current CONFIG to file (only configurable keys)."""
    path = Path(config_path) if config_path else CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {k: CONFIG[k] for k in sorted(_CONFIGURABLE_KEYS)
            if k in CONFIG and CONFIG[k] is not None}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def run_setup(first_run=False):
    """Interactive setup wizard. Runs on first use or via --init."""
    if first_run:
        print("\n  Welcome to bluray-pipeline!")
        print("  No config file found — let's set things up.\n")
    else:
        print("\n  bluray-pipeline — Setup")
    print("  " + "=" * 40)

    # Auto-detect what we can before asking
    _detect_defaults()

    # 1. Media root directory
    default_base = CONFIG.get("output_base") or str(Path.home() / "Media")
    print("\n  Where should media files be stored?")
    print("  This directory will contain Movies/, Shows/, _rips/, _logs/, _cache/")
    base = input(f"  Media root [{default_base}]: ").strip() or default_base
    _derive_paths(base)

    # 2. MakeMKV path
    default_mkv = CONFIG.get("makemkv_bin") or "makemkvcon"
    found_msg = " (auto-detected)" if Path(default_mkv).exists() else ""
    mkv_bin = input(f"\n  MakeMKV path [{default_mkv}]{found_msg}: ").strip() or default_mkv
    CONFIG["makemkv_bin"] = mkv_bin

    # 3. HandBrake path
    default_hb = CONFIG.get("handbrake_bin") or "HandBrakeCLI"
    found_msg = " (auto-detected)" if _find_binary(default_hb) else ""
    hb_bin = input(f"  HandBrakeCLI path [{default_hb}]{found_msg}: ").strip() or default_hb
    CONFIG["handbrake_bin"] = hb_bin

    # 4. Viewing profile (replaces encoder choice)
    print("\n  Primary viewing device:")
    print("    1. OLED TV — best quality, largest files (home theater)")
    print("    2. LED TV — high quality, balanced size")
    print("    3. Tablet — good quality, smaller files")
    print("    4. Phone — decent quality, smallest files")
    print("    5. Mixed — balanced for multiple devices (recommended)")
    profile_choice = input("  Choice [5]: ").strip()
    profile_map = {"1": "oled", "2": "led", "3": "tablet", "4": "phone", "5": "mixed"}
    profile_name = profile_map.get(profile_choice, "mixed")
    CONFIG["viewing_profile"] = profile_name
    profile = _VIEWING_PROFILES[profile_name]
    CONFIG["quality_rf_uhd"] = profile["quality_rf_uhd"]
    CONFIG["quality_rf_bd"] = profile["quality_rf_bd"]
    CONFIG["quality_rf_dvd"] = profile["quality_rf_dvd"]
    CONFIG["audio_mode"] = profile["audio_mode"]
    CONFIG["encoder"] = "vt_h265_10bit"
    CONFIG["quality_rf"] = profile["quality_rf_bd"]  # Default RF for display
    CONFIG["encoder_preset"] = "quality"
    CONFIG["hq_mode"] = False

    # 5. TMDb API key
    print("\n  TMDb API key (free at https://www.themoviedb.org/settings/api)")
    current_tmdb = CONFIG.get("tmdb_api_key", "")
    if current_tmdb:
        masked = current_tmdb[:4] + "..." + current_tmdb[-4:]
        tmdb = input(f"  TMDb API key [{masked}]: ").strip() or current_tmdb
    else:
        tmdb = input("  TMDb API key: ").strip()
    CONFIG["tmdb_api_key"] = tmdb

    # 6. Jellyfin
    default_jf = CONFIG.get("jellyfin_url", "http://localhost:8096")
    jf_url = input(f"\n  Jellyfin URL [{default_jf}]: ").strip() or default_jf
    CONFIG["jellyfin_url"] = jf_url

    current_jf = CONFIG.get("jellyfin_api_key", "")
    if current_jf:
        masked = current_jf[:4] + "..." + current_jf[-4:]
        jf_key = input(f"  Jellyfin API key [{masked}]: ").strip() or current_jf
    else:
        jf_key = input("  Jellyfin API key (optional, Enter to skip): ").strip()
    CONFIG["jellyfin_api_key"] = jf_key

    # Save
    path = save_config()
    print(f"\n  Config saved to {path}")
    print("  Run --status to verify everything works.")
    print()


# ============================================================================
# LOGGING
# ============================================================================

class _TUILogHandler(logging.Handler):
    """Routes log messages to the TUI when active, otherwise does nothing."""
    def emit(self, record):
        if tui and tui.enabled:
            msg = record.getMessage()
            # Strip leading whitespace/decoration for cleaner TUI display
            msg = msg.strip()
            if msg:
                tui.log(msg)


_stdout_handler = None  # Module-level ref so TUI can suppress/restore


def setup_logging():
    global _stdout_handler
    _stdout_handler = logging.StreamHandler(sys.stdout)
    handlers = [_stdout_handler]

    # Try to set up file logging, fall back to console-only
    log_dir = Path(CONFIG["log_dir"])
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass  # No file logging — media drive may not be mounted

    # Add TUI handler (only emits when TUI is active)
    handlers.append(_TUILogHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("bluray_pipeline")


log = None  # Initialized in main()

# ============================================================================
# DRIVE DETECTION & COMPATIBILITY CHECK
# ============================================================================

def check_drive():
    """Detect the Blu-ray drive and check LibreDrive / UHD compatibility."""
    makemkv = CONFIG["makemkv_bin"]

    if not Path(makemkv).exists():
        log.error(f"MakeMKV not found at {makemkv}")
        log.error("Install with: brew install --cask makemkv")
        return None

    log.info("Scanning for optical drives...")
    result = run_cmd([makemkv, "-r", "info", "disc:9999"], timeout=120)

    if result is None:
        log.error("Failed to query MakeMKV for drive info.")
        return None

    # Parse drive info from MakeMKV output
    drive_info = {
        "found": False,
        "index": None,
        "name": None,
        "firmware": None,
        "libredrive": None,
        "uhd_capable": False,
    }

    for line in result.stdout.splitlines():
        # DRV line format: DRV:index,visible,enabled,flags,"drive_name","disc_label","/dev/..."
        # Example: DRV:0,2,999,12,"BD-RE PIONEER BD-RW BDR-UD04 1.14 ...","THE_REVENANT","/dev/rdisk8"
        if line.startswith("DRV:") and "/dev/" in line:
            parts = line.split(",")
            if len(parts) >= 7:
                drive_info["found"] = True
                drive_info["index"] = parts[0].split(":")[1]
                drive_info["name"] = parts[4].strip('"')      # Drive model
                drive_info["disc_label"] = parts[5].strip('"') # Disc label
                drive_info["dev_path"] = parts[6].strip('"')   # /dev/rdiskN

        # Parse MSG lines for drive model and LibreDrive info from disc:9999 output
        if "opened in OS access mode" in line:
            # Extract drive model from: MSG:2010,...,"Optical drive \"MODEL\" opened..."
            match = re.search(r'"Optical drive \\"(.+?)\\"', line)
            if match:
                drive_info["firmware"] = match.group(1)

    if not drive_info["found"]:
        log.warning("No optical drive detected. Is a disc inserted?")
        return drive_info

    # Now do a full disc scan only if --check-drive was requested (not for normal runs)
    # For normal pipeline runs, we already have enough info from disc:9999
    # LibreDrive status requires reading from the disc, which is slow
    # We'll get it during the actual rip instead

    # Report what we know from the quick scan
    log.info("=" * 60)
    log.info("DRIVE COMPATIBILITY REPORT")
    log.info("=" * 60)
    log.info(f"  Drive:      {drive_info['name']}")
    log.info(f"  Device:     {drive_info.get('dev_path', 'Unknown')}")
    if drive_info.get("disc_label"):
        log.info(f"  Disc:       {drive_info['disc_label']}")
    if drive_info.get("firmware"):
        log.info(f"  Firmware:   {drive_info['firmware']}")

    # Pioneer BDR-UD04 is a known LibreDrive drive
    if "BDR-UD04" in drive_info["name"] or "BDR-US04" in drive_info["name"]:
        drive_info["libredrive"] = True
        drive_info["uhd_capable"] = True
        log.info("  LibreDrive: ✅ ENABLED (Pioneer BDR-UD04 — known good)")
    else:
        log.info("  LibreDrive: will be checked during rip")

    log.info("=" * 60)
    return drive_info


# ============================================================================
# HELPERS
# ============================================================================

def run_cmd(cmd, timeout=None, capture=True):
    """Run a shell command and return the result."""
    if _shutdown_requested:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        log.error(f"Command timed out: {' '.join(cmd[:3])}...")
        return None
    except FileNotFoundError:
        log.error(f"Command not found: {cmd[0]}")
        return None


def run_cmd_progress(cmd, timeout=None, progress_handler=None):
    """
    Run a command with real-time stdout streaming for progress reporting.
    Thread-safe: multiple calls can run concurrently (e.g. rip + encode).
    progress_handler: callable(line) that processes each stdout line.
    Returns (returncode, stderr_text).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line-buffered
        )
        with _active_processes_lock:
            _active_processes.append(proc)

        # Drain stderr in a background thread to prevent pipe buffer deadlocks
        # (HandBrake can write a lot to stderr during encoding)
        stderr_chunks = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        start = time.time()

        for line in proc.stdout:
            if _shutdown_requested:
                proc.terminate()
                proc.wait(timeout=10)
                return None, "Cancelled by user"
            if timeout and (time.time() - start) > timeout:
                proc.kill()
                log.error(f"Command timed out after {timeout}s")
                return None, ""
            line = line.rstrip()
            if progress_handler:
                progress_handler(line)

        proc.wait()
        stderr_thread.join(timeout=5)
        with _active_processes_lock:
            if proc in _active_processes:
                _active_processes.remove(proc)
        stderr_text = "".join(stderr_chunks)
        return proc.returncode, stderr_text

    except FileNotFoundError:
        log.error(f"Command not found: {cmd[0]}")
        return None, ""


def makemkv_progress(line):
    """Parse MakeMKV robot-mode progress lines and display them."""
    # PRGV:current,total,max — overall progress
    if line.startswith("PRGV:"):
        parts = line[5:].split(",")
        if len(parts) >= 3:
            current = int(parts[0])
            total = int(parts[2])
            if total > 0:
                pct = (current / total) * 100
                if tui and tui.enabled:
                    tui.update_rip(pct)
                else:
                    print(f"\r  Ripping: {pct:5.1f}% complete", end="", flush=True)
    # PRGT: — current task name
    elif line.startswith("PRGT:"):
        task = line.split(",")[-1].strip('"')
        if task:
            if tui and tui.enabled:
                tui.update_rip(tui.rip_pct, task=task)
            else:
                print(f"\r  {task:<60}", end="", flush=True)
    # MSG: — status messages (only show important ones)
    elif line.startswith("MSG:") and any(kw in line for kw in ["error", "Error", "LibreDrive"]):
        parts = line.split(",", 4)
        if len(parts) >= 5:
            msg = parts[3].strip('"')
            if tui and tui.enabled:
                tui.log(msg)
            else:
                log.info(f"  {msg}")


def handbrake_progress(line):
    """Parse HandBrake CLI progress output."""
    if "Encoding:" in line and "%" in line:
        match = re.search(r'(\d+\.\d+)\s*%.*?ETA\s*(\d+h\d+m\d+s)', line)
        if match:
            pct = float(match.group(1))
            eta = match.group(2)
            if tui and tui.enabled:
                tui.update_encode(pct, eta=eta)
            else:
                print(f"\r  Encoding: {pct:5.1f}% — ETA {eta}  ", end="", flush=True)
        else:
            match = re.search(r'(\d+\.\d+)\s*%', line)
            if match:
                pct = float(match.group(1))
                if tui and tui.enabled:
                    tui.update_encode(pct)
                else:
                    print(f"\r  Encoding: {pct:5.1f}%  ", end="", flush=True)
    elif "work result" in line.lower() or "Muxing" in line:
        msg = line.strip()
        if tui and tui.enabled:
            tui.log(msg)
        else:
            print(f"\n  {msg}")


def detect_source_format(scan_output):
    """
    Detect the source format (DVD, Blu-ray, UHD) from MakeMKV scan output.
    Parses CINFO and TINFO lines to determine disc type and video resolution.

    Returns dict with:
      disc_type: "dvd" | "bluray" | "uhd" | "unknown"
      resolution: (width, height) or None
      interlaced: True/False (detected from TINFO video description)
    """
    info = {"disc_type": "unknown", "resolution": None, "interlaced": False}
    if not scan_output:
        return info

    max_res_pixels = 0

    for line in scan_output.splitlines():
        # CINFO:1,6209,"Blu-ray disc"  or  CINFO:1,6210,"DVD disc"
        # CINFO:1 is the disc type
        if line.startswith("CINFO:1,"):
            lower = line.lower()
            if "dvd" in lower:
                info["disc_type"] = "dvd"
            elif "uhd" in lower or "4k" in lower:
                info["disc_type"] = "uhd"
            elif "blu-ray" in lower or "bd" in lower:
                info["disc_type"] = "bluray"

        # TINFO:tid,19,0,"resolution_description"
        # TINFO:tid,20,0,"video_codec_description"
        # SINFO:tid,sid,19,0,"1920x1080"  — stream-level resolution
        # SINFO:tid,sid,20,0,"MPEG2" or "H.264" etc
        if "SINFO:" in line:
            # Look for resolution in stream info (attribute 19)
            res_match = re.search(r'(\d{3,5})x(\d{3,5})', line)
            if res_match:
                w, h = int(res_match.group(1)), int(res_match.group(2))
                pixels = w * h
                if pixels > max_res_pixels:
                    max_res_pixels = pixels
                    info["resolution"] = (w, h)

            # Check for interlaced indicator
            if any(marker in line.lower() for marker in ["interlaced", "1080i", "576i", "480i"]):
                info["interlaced"] = True

    # Infer disc type from resolution if CINFO didn't tell us
    if info["disc_type"] == "unknown" and info["resolution"]:
        w, h = info["resolution"]
        if h <= 576:
            info["disc_type"] = "dvd"
        elif h <= 1080:
            info["disc_type"] = "bluray"
        elif h >= 2160:
            info["disc_type"] = "uhd"

    # DVDs are commonly interlaced even if not explicitly flagged
    if info["disc_type"] == "dvd":
        info["interlaced"] = True  # Safer to always deinterlace DVDs

    return info


def auto_tune_for_source(source_info):
    """
    Automatically adjust encoder settings based on source format.
    Uses per-source RF values from the viewing profile (quality_rf_dvd/bd/uhd).
    All sources use VideoToolbox by default. DVDs get deinterlace.

    Respects manual overrides: --hq forces software x265, --quality skips RF assignment.
    """
    disc_type = source_info.get("disc_type", "unknown")
    res = source_info.get("resolution")
    res_str = f"{res[0]}x{res[1]}" if res else "unknown"

    log.info(f"  Source: {disc_type.upper()} ({res_str})")

    # Don't override user's explicit --hq choice
    if CONFIG.get("_user_set_hq"):
        log.info("  Encoder: user override (--hq)")
        # Still enable deinterlace for DVDs even in --hq mode
        if disc_type == "dvd":
            CONFIG["deinterlace"] = True
            log.info("  Deinterlace: auto-enabled for DVD source")
        return

    # Pick per-source RF (unless user passed --quality explicitly)
    if not CONFIG.get("_user_set_quality"):
        if disc_type == "dvd":
            CONFIG["quality_rf"] = CONFIG["quality_rf_dvd"]
        elif disc_type == "bluray":
            CONFIG["quality_rf"] = CONFIG["quality_rf_bd"]
        elif disc_type == "uhd":
            CONFIG["quality_rf"] = CONFIG["quality_rf_uhd"]
    else:
        log.info("  RF: user override (--quality)")

    # All sources use VideoToolbox (no DVD→software switch)
    CONFIG["encoder"] = "vt_h265_10bit"
    CONFIG["encoder_preset"] = "quality"
    CONFIG["encoder_level"] = "auto"
    CONFIG["hq_mode"] = False

    if disc_type == "dvd":
        CONFIG["deinterlace"] = True
        log.info(f"  Auto-tuned: DVD mode (VideoToolbox @ RF {CONFIG['quality_rf']}, deinterlace on)")
    elif disc_type == "bluray":
        CONFIG["deinterlace"] = False
        log.info(f"  Auto-tuned: Blu-ray mode (VideoToolbox @ RF {CONFIG['quality_rf']})")
    elif disc_type == "uhd":
        CONFIG["deinterlace"] = False
        log.info(f"  Auto-tuned: UHD mode (VideoToolbox @ RF {CONFIG['quality_rf']})")
        log.info("  HDR: automatic passthrough (HDR10/Dolby Vision preserved by HandBrake)")
    else:
        log.info("  Unknown source — using default settings")

    # Update TUI with source/encoder info
    if tui and tui.enabled:
        is_hq = CONFIG.get("hq_mode", False)
        enc_label = f"x265 RF {CONFIG['quality_rf']}" if is_hq else f"VideoToolbox RF {CONFIG['quality_rf']}"
        tui.set_metadata(
            tui.title, tui.year, tui.media_type,
            source_format=f"{disc_type.upper()} {res_str}",
            encoder_mode=enc_label,
        )


def sanitize_filename(name):
    """Clean a string for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def disc_is_inserted():
    """Check if a disc is currently in the drive."""
    result = run_cmd(["drutil", "status"], timeout=10)
    if result and result.returncode == 0:
        return "No Media Inserted" not in result.stdout
    return False


def eject_disc():
    """Eject the disc after processing."""
    log.info("Ejecting disc...")
    run_cmd(["drutil", "eject"], timeout=10)


def validate_api_keys():
    """
    Validate that required API keys are set and working.
    Checks TMDB_API_KEY and JELLYFIN_API_KEY with a lightweight API call.
    Returns True if all keys are valid, False otherwise.
    """
    ok = True

    # --- TMDb ---
    tmdb_key = CONFIG["tmdb_api_key"]
    if not tmdb_key:
        log.error("TMDB_API_KEY is not set.")
        log.error("  1. Sign up at themoviedb.org")
        log.error("  2. Go to Settings > API > copy your v3 key")
        log.error("  3. export TMDB_API_KEY=\"your_key\"")
        ok = False
    else:
        try:
            url = f"https://api.themoviedb.org/3/configuration?api_key={tmdb_key}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    log.info("TMDb API key: valid")
                else:
                    log.error(f"TMDb API key: invalid (HTTP {resp.status})")
                    ok = False
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log.error("TMDb API key: invalid (unauthorized). Check your key.")
            else:
                log.error(f"TMDb API key: check failed (HTTP {e.code})")
            ok = False
        except (urllib.error.URLError, OSError) as e:
            log.warning(f"TMDb API key: could not verify (network error: {e})")
            log.warning("  Continuing anyway — metadata lookup may fail.")

    # --- Jellyfin ---
    jf_key = CONFIG["jellyfin_api_key"]
    jf_url = CONFIG["jellyfin_url"]
    if not jf_key:
        log.warning("JELLYFIN_API_KEY is not set — library auto-scan disabled.")
        log.warning("  Dashboard > API Keys > create one, then:")
        log.warning("  export JELLYFIN_API_KEY=\"your_key\"")
        # Not a hard failure — pipeline works without Jellyfin
    else:
        try:
            url = f"{jf_url}/System/Info"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f'MediaBrowser Token="{jf_key}"'},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    server = data.get("ServerName", "Jellyfin")
                    version = data.get("Version", "?")
                    log.info(f"Jellyfin API key: valid ({server} v{version})")
                else:
                    log.warning(f"Jellyfin API key: unexpected response (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log.error("Jellyfin API key: invalid (unauthorized). Check your key.")
                log.error("  Dashboard > API Keys to verify.")
            else:
                log.warning(f"Jellyfin API key: check failed (HTTP {e.code})")
        except (urllib.error.URLError, OSError):
            log.warning(f"Jellyfin not reachable at {jf_url} — is it running?")
            log.warning("  Library scan will be skipped.")

    return ok


def ensure_dirs():
    """Create output directories if they don't exist."""
    for key in ["rip_dir", "encode_dir", "tv_encode_dir", "log_dir"]:
        Path(CONFIG[key]).mkdir(parents=True, exist_ok=True)


def check_external_drive():
    """Verify the external drive is mounted."""
    base = Path(CONFIG["output_base"])
    if not base.exists():
        log.error(f"External drive not found at {base}")
        log.error("Make sure EXTNVMESSD1 is connected and mounted.")
        return False
    free_gb = shutil.disk_usage(base).free / (1024 ** 3)
    log.info(f"External drive mounted. Free space: {free_gb:.1f} GB")
    if free_gb < 80:
        log.warning("Low disk space! A 4K rip + encode can need 60-100 GB temporarily.")
    return True


# ============================================================================
# DISC METADATA DETECTION
# ============================================================================

def extract_disc_label():
    """
    Extract the disc volume label from MakeMKV's robot-mode output.
    Returns the raw label string, e.g. "BLADE_RUNNER_2049" or "THE_MATRIX_4K".
    """
    makemkv = CONFIG["makemkv_bin"]
    log.info("Reading disc volume label...")

    result = run_cmd([makemkv, "-r", "info", "disc:0"], timeout=120)
    if result is None:
        return None

    disc_name = None
    cinfo_name = None

    for line in result.stdout.splitlines():
        # CINFO:2,0,"disc_label"  — attribute 2 is the volume name
        if line.startswith("CINFO:2,"):
            match = re.match(r'CINFO:2,\d+,"(.*)"', line)
            if match:
                cinfo_name = match.group(1)
        # CINFO:32,0,"human_readable_name" — attribute 32 is a friendly name
        if line.startswith("CINFO:32,"):
            match = re.match(r'CINFO:32,\d+,"(.*)"', line)
            if match:
                disc_name = match.group(1)

    # Prefer the human-readable name, fall back to volume label
    label = disc_name or cinfo_name
    if label:
        log.info(f"  Disc label: {label}")
    else:
        log.warning("  Could not read disc label.")
    return label


def clean_disc_label(raw_label):
    """
    Convert a raw disc label like 'BLADE_RUNNER_2049_4KUHD' into a search-
    friendly string like 'Blade Runner 2049'.

    Strips common suffixes: _4KUHD, _UHD, _BLURAY, _BD, disc numbers, region codes.
    Also detects TV season/disc indicators and returns them separately.

    Returns (cleaned_label, season_num_or_None, disc_num_or_None).
    """
    if not raw_label:
        return None, None, None

    label = raw_label.upper()

    # Try to extract season number from common patterns
    season_num = None
    disc_num = None

    season_patterns = [
        r'_?S(\d{1,2})_?(?:D|DISC|DISK)?_?(\d+)?',   # S01, S01D1, S1_DISC_2
        r'_?SEASON_?(\d{1,2})_?(?:D|DISC|DISK)?_?(\d+)?',
        r'_?COMPLETE_?SERIES',
        r'_?SERIES_?(\d{1,2})',
    ]
    for pattern in season_patterns:
        match = re.search(pattern, label, re.IGNORECASE)
        if match:
            groups = match.groups()
            if groups[0] and groups[0].isdigit():
                season_num = int(groups[0])
            if len(groups) > 1 and groups[1] and groups[1].isdigit():
                disc_num = int(groups[1])
            # Remove the matched season/disc text from label
            label = re.sub(pattern, '', label, flags=re.IGNORECASE)
            break

    # If no explicit season but has disc number, extract it
    if disc_num is None:
        disc_match = re.search(r'_?(?:DISC|DISK|D)_?(\d+)', label, re.IGNORECASE)
        if disc_match:
            disc_num = int(disc_match.group(1))
            label = re.sub(r'_?(?:DISC|DISK|D)_?\d+', '', label, flags=re.IGNORECASE)

    # Remove common Blu-ray/DVD suffixes
    # IMPORTANT: Use word boundaries (\b or _ prefix) to avoid mangling labels
    # like "BBCDVD1219" where "DVD" is part of the label, not a suffix
    strip_patterns = [
        r'[_\s]4K[_\s]?UHD',
        r'[_\s]UHD\b',
        r'[_\s]BLU[_\s]?RAY',
        r'[_\s]BLURAY',
        r'[_\s]BD[_\s]?\d*$',
        r'[_\s]FPL[_\s]\d+',
        r'[_\s]CEE?$',
        r'[_\s]USA?$',
        r'[_\s]GBR?$',
        r'[_\s]EUR?$',
        r'[_\s]REG[_\s]?[A-C]',
        r'[_\s]DVD\d?$',           # Only strip DVD at end of label
        r'[_\s]COMPLETE[_\s]?SERIES',
    ]
    for pattern in strip_patterns:
        label = re.sub(pattern, '', label, flags=re.IGNORECASE)

    # Strip trailing numbers that look like catalog codes (e.g. "1219" in "BBCDVD1219")
    # but NOT numbers that are part of a title (e.g. "2049" in "BLADE_RUNNER_2049")
    # Heuristic: only strip if the remaining label is very short or all-alpha
    stripped = re.sub(r'\d+$', '', label).strip('_').strip()
    if stripped and len(stripped) >= 3:
        # Check if the numbers look like a year (4 digits, 19xx or 20xx)
        trailing = label[len(stripped):].strip('_').strip()
        if trailing and not re.match(r'^(19|20)\d{2}$', trailing):
            label = stripped

    # Replace underscores with spaces
    label = label.replace('_', ' ')

    # Collapse multiple spaces, strip
    label = re.sub(r'\s+', ' ', label).strip()

    # Title-case it
    label = label.title()

    log.info(f"  Cleaned label: {label}")
    if season_num is not None:
        log.info(f"  Detected season: {season_num}" + (f", disc {disc_num}" if disc_num else ""))

    return label, season_num, disc_num


def tmdb_search(query):
    """
    Search TMDb for a movie matching the query string.
    Returns dict with: title, year, overview, tmdb_id, poster_path
    or None if no match / no API key.
    """
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        log.warning("No TMDb API key set. Skipping metadata lookup.")
        log.warning("Get a free key: https://www.themoviedb.org/settings/api")
        log.warning("Then: export TMDB_API_KEY=\"your_key\"")
        return None

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/movie"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    log.info(f"  Searching TMDb for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb search failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        log.warning(f"  No TMDb results for '{query}'")
        return None

    # Take the first (most relevant) result
    hit = results[0]
    release_date = hit.get("release_date", "")
    year = release_date[:4] if release_date else None

    metadata = {
        "title": hit.get("title", query),
        "year": year,
        "overview": hit.get("overview", ""),
        "tmdb_id": hit.get("id"),
        "poster_path": hit.get("poster_path"),
        "original_title": hit.get("original_title"),
        "vote_average": hit.get("vote_average"),
        "genres": [],
        "directors": [],
        "cast": [],
        "studio": None,
        "tagline": None,
        "runtime": None,
        "certification": None,
    }

    log.info(f"  TMDb match: {metadata['title']} ({metadata['year']})")
    if len(results) > 1:
        log.info(f"  ({len(results) - 1} other candidates — use --title to override)")
        for alt in results[1:4]:
            alt_year = alt.get("release_date", "")[:4]
            log.info(f"    - {alt.get('title')} ({alt_year})")

    # Fetch full details (credits, genres, certifications) in one call
    metadata = tmdb_fetch_details(metadata)

    return metadata


def tmdb_dual_search(query):
    """
    Search TMDb for both movies and TV shows using /search/multi.
    Returns (metadata_dict, "movie"|"tv") or (None, "movie") if no results.
    """
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        return None, "movie"

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/multi"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    log.info(f"  Searching TMDb (movie + TV) for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb multi-search failed, trying movie search: {e}")
        meta = tmdb_search(query)
        return meta, "movie"

    # Filter to movie and tv only (multi also returns person results)
    results = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]

    if not results:
        log.warning(f"  No TMDb results for '{query}'")
        return None, "movie"

    hit = results[0]
    detected_type = hit.get("media_type", "movie")

    # Log the best match
    if detected_type == "tv":
        title = hit.get("name", query)
        year = (hit.get("first_air_date") or "")[:4] or None
    else:
        title = hit.get("title", query)
        year = (hit.get("release_date") or "")[:4] or None

    log.info(f"  Best match [{detected_type.upper()}]: {title} ({year})")

    # Show runner-up if different type (helps user spot misdetections)
    for alt in results[1:5]:
        alt_type = alt.get("media_type", "?")
        if alt_type != detected_type:
            alt_title = alt.get("name") or alt.get("title")
            alt_year = (alt.get("first_air_date") or alt.get("release_date") or "")[:4]
            log.info(f"  Also found [{alt_type.upper()}]: {alt_title} ({alt_year})")
            break

    # Fetch full details via the type-specific search (which also fetches details)
    if detected_type == "tv":
        meta = tmdb_search_tv(query)
    else:
        meta = tmdb_search(query)

    return meta, detected_type


def tmdb_fetch_details(metadata):
    """
    Fetch full movie details from TMDb: cast, directors, genres, studio,
    runtime, tagline, content rating.
    Uses the append_to_response trick to do it in a single API call.
    """
    api_key = CONFIG["tmdb_api_key"]
    tmdb_id = metadata.get("tmdb_id")
    if not api_key or not tmdb_id:
        return metadata

    url = (
        f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        f"?api_key={api_key}"
        f"&append_to_response=credits,release_dates"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb details fetch failed: {e}")
        return metadata

    # Genres
    metadata["genres"] = [g["name"] for g in data.get("genres", [])]

    # Studio (first production company)
    companies = data.get("production_companies", [])
    if companies:
        metadata["studio"] = companies[0].get("name")

    # Runtime, tagline, backdrop
    metadata["runtime"] = data.get("runtime")
    metadata["tagline"] = data.get("tagline")
    metadata["backdrop_path"] = data.get("backdrop_path")

    # Credits: directors and top-billed cast
    credits = data.get("credits", {})

    directors = [
        p["name"] for p in credits.get("crew", [])
        if p.get("job") == "Director"
    ]
    metadata["directors"] = directors

    cast_list = credits.get("cast", [])[:15]  # Top 15 actors
    metadata["cast"] = [
        {"name": a["name"], "role": a.get("character", "")}
        for a in cast_list
    ]

    # Content rating (US certification)
    release_dates = data.get("release_dates", {}).get("results", [])
    for country in release_dates:
        if country.get("iso_3166_1") == "US":
            certs = country.get("release_dates", [])
            for cert in certs:
                if cert.get("certification"):
                    metadata["certification"] = cert["certification"]
                    break
            break

    # Log summary
    if directors:
        log.info(f"  Director(s): {', '.join(directors)}")
    if metadata["genres"]:
        log.info(f"  Genres: {', '.join(metadata['genres'])}")
    if metadata["cast"]:
        top3 = [a["name"] for a in metadata["cast"][:3]]
        log.info(f"  Cast: {', '.join(top3)}, ...")
    if metadata["certification"]:
        log.info(f"  Rated: {metadata['certification']}")

    return metadata


def tmdb_search_tv(query):
    """
    Search TMDb for a TV show matching the query string.
    Returns dict with: title, year, overview, tmdb_id, poster_path, seasons
    or None if no match / no API key.
    """
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        return None

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/tv"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    log.info(f"  Searching TMDb TV for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb TV search failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        log.warning(f"  No TMDb TV results for '{query}'")
        return None

    hit = results[0]
    first_air = hit.get("first_air_date", "")
    year = first_air[:4] if first_air else None

    metadata = {
        "media_type": "tv",
        "title": hit.get("name", query),
        "year": year,
        "overview": hit.get("overview", ""),
        "tmdb_id": hit.get("id"),
        "poster_path": hit.get("poster_path"),
        "original_title": hit.get("original_name"),
        "vote_average": hit.get("vote_average"),
        "genres": [],
        "directors": [],
        "cast": [],
        "studio": None,
        "tagline": None,
        "runtime": None,
        "certification": None,
        "seasons": [],
        "backdrop_path": hit.get("backdrop_path"),
    }

    log.info(f"  TMDb TV match: {metadata['title']} ({metadata['year']})")
    if len(results) > 1:
        log.info(f"  ({len(results) - 1} other candidates)")
        for alt in results[1:4]:
            alt_year = alt.get("first_air_date", "")[:4]
            log.info(f"    - {alt.get('name')} ({alt_year})")

    # Fetch full show details
    metadata = tmdb_fetch_tv_details(metadata)

    return metadata


def tmdb_fetch_tv_details(metadata):
    """Fetch full TV show details: genres, cast, seasons, network."""
    api_key = CONFIG["tmdb_api_key"]
    tmdb_id = metadata.get("tmdb_id")
    if not api_key or not tmdb_id:
        return metadata

    url = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        f"?api_key={api_key}"
        f"&append_to_response=credits,content_ratings"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb TV details fetch failed: {e}")
        return metadata

    metadata["genres"] = [g["name"] for g in data.get("genres", [])]

    networks = data.get("networks", [])
    if networks:
        metadata["studio"] = networks[0].get("name")

    metadata["tagline"] = data.get("tagline")
    metadata["backdrop_path"] = data.get("backdrop_path")

    # Seasons info
    seasons = data.get("seasons", [])
    metadata["seasons"] = [
        {
            "season_number": s.get("season_number"),
            "name": s.get("name"),
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
        }
        for s in seasons
        if s.get("season_number", 0) > 0  # Skip specials (season 0)
    ]

    # Credits
    credits = data.get("credits", {})
    cast_list = credits.get("cast", [])[:15]
    metadata["cast"] = [
        {"name": a["name"], "role": a.get("character", "")}
        for a in cast_list
    ]

    # Creators as "directors"
    creators = data.get("created_by", [])
    metadata["directors"] = [c["name"] for c in creators]

    # Content rating (US)
    content_ratings = data.get("content_ratings", {}).get("results", [])
    for cr in content_ratings:
        if cr.get("iso_3166_1") == "US":
            metadata["certification"] = cr.get("rating")
            break

    if metadata["seasons"]:
        log.info(f"  Seasons: {len(metadata['seasons'])}")
    if metadata["directors"]:
        log.info(f"  Created by: {', '.join(metadata['directors'])}")
    if metadata["genres"]:
        log.info(f"  Genres: {', '.join(metadata['genres'])}")

    return metadata


def tmdb_get_season_episodes(tmdb_id, season_num):
    """Fetch episode list for a specific season from TMDb."""
    api_key = CONFIG["tmdb_api_key"]
    if not api_key or not tmdb_id:
        return []

    url = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}"
        f"?api_key={api_key}"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"  TMDb season fetch failed: {e}")
        return []

    episodes = data.get("episodes", [])
    return [
        {
            "episode_number": ep.get("episode_number"),
            "name": ep.get("name", ""),
            "overview": ep.get("overview", ""),
            "air_date": ep.get("air_date", ""),
            "runtime": ep.get("runtime"),
        }
        for ep in episodes
    ]


def load_metadata_cache():
    """Load the persistent metadata cache from disk."""
    cache_path = Path(CONFIG["metadata_cache"])
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_metadata_cache(cache):
    """Save metadata cache to disk."""
    cache_path = Path(CONFIG["metadata_cache"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))


def get_disc_metadata(manual_title=None, manual_year=None, media_type=None,
                      season=None, start_episode=None):
    """
    Main metadata resolution flow:
      1. If --title was given, use that (optionally search TMDb for year)
      2. Otherwise, read the disc label
      3. Detect if TV or movie based on label patterns or --tv flag
      4. Check local cache for a previous lookup of this label
      5. Search TMDb (movie or TV) for metadata
      6. Fall back to cleaned disc label if TMDb fails
      7. In interactive mode, confirm with user

    Returns dict with keys:
      title, year, media_type ("movie"|"tv"), season, start_episode, metadata
    """
    log.info("=" * 60)
    log.info("METADATA DETECTION")
    log.info("=" * 60)

    cache = load_metadata_cache()
    result = {
        "title": None, "year": None, "media_type": media_type or "auto",
        "season": season, "start_episode": start_episode, "metadata": None,
        "_user_set_type": media_type is not None,  # True if --tv or --movie used
    }

    # If user gave an explicit title, optionally enrich with TMDb
    if manual_title:
        if media_type == "tv":
            meta = tmdb_search_tv(manual_title)
        elif media_type == "movie":
            meta = tmdb_search(manual_title)
        else:
            # No type specified — dual search to auto-detect
            meta, detected = tmdb_dual_search(manual_title)
            result["media_type"] = detected
        if meta:
            result["title"] = meta["title"]
            result["year"] = meta.get("year") or manual_year
            result["metadata"] = meta
            if meta.get("media_type"):
                result["media_type"] = meta["media_type"]
        else:
            result["title"] = manual_title
            result["year"] = manual_year
        return result

    # Read disc label
    raw_label = extract_disc_label()
    if not raw_label:
        log.warning("No disc label detected.")
        if sys.stdin.isatty():
            result["title"] = input("\nEnter the movie/show title: ").strip()
            result["year"] = input("Enter the year (or Enter to skip): ").strip() or None
            mtype = input("Type [m]ovie or [t]v? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
                s = input("Season number: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
                ep = input("Starting episode number [1]: ").strip()
                result["start_episode"] = int(ep) if ep.isdigit() else 1
        return result

    # Check cache — re-fetch TMDb metadata if we have a cached tmdb_id
    if raw_label in cache:
        cached = cache[raw_label]
        log.info(f"  Cache hit: {cached['title']} ({cached.get('year')})")
        result.update(cached)
        tmdb_id = cached.get("tmdb_id")
        if tmdb_id:
            mtype = cached.get("media_type", "movie")
            if mtype == "tv":
                meta = tmdb_search_tv(cached["title"])
            else:
                meta = tmdb_search(cached["title"])
            if meta:
                result["metadata"] = meta
        return result

    # Clean label — returns (cleaned, season_num, disc_num)
    cleaned, detected_season, detected_disc = clean_disc_label(raw_label)

    # If season detected in label, this is definitely a TV disc
    if detected_season is not None and media_type is None:
        result["media_type"] = "tv"
        result["season"] = season or detected_season
        log.info(f"  Auto-detected as TV show (season {result['season']})")

    # Search TMDb
    meta = None
    if result["media_type"] == "tv":
        # Already know it's TV (from --tv flag or label pattern)
        meta = tmdb_search_tv(cleaned) if cleaned else None
    elif media_type == "movie":
        # Explicitly told it's a movie
        meta = tmdb_search(cleaned) if cleaned else None
    else:
        # Unknown type — use dual search to auto-detect
        if cleaned:
            meta, detected_type = tmdb_dual_search(cleaned)
            result["media_type"] = detected_type
            if detected_type == "tv" and not result.get("season"):
                result["season"] = season or 1

    if meta:
        result["title"] = meta["title"]
        result["year"] = meta.get("year")
        result["metadata"] = meta
    elif sys.stdin.isatty():
        # TMDb found nothing — disc label is probably useless (e.g. "BBCDVD", "DISC1")
        # Ask the user for the real title and search again
        log.warning(f"  No TMDb match for disc label '{raw_label}'.")
        print(f"\n  The disc label '{raw_label}' didn't match anything on TMDb.")
        print("  Please enter the title manually so we can look it up.\n")

        manual_title = input("  Title: ").strip()
        if not manual_title:
            result["title"] = cleaned or raw_label
        else:
            mtype = input("  Is this a [m]ovie or [t]v show? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
                s = input("  Season number [1]: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
            result["_user_set_type"] = True  # User explicitly chose type

            # Re-search TMDb with the user's title
            if result["media_type"] == "tv":
                meta = tmdb_search_tv(manual_title)
            else:
                meta, detected_type = tmdb_dual_search(manual_title)
                if detected_type == "tv" and result["media_type"] != "tv":
                    print(f"  TMDb thinks this is a TV show. Switch to TV mode? [Y/n]: ", end="")
                    if input().strip().lower() != 'n':
                        result["media_type"] = "tv"
                        if not result.get("season"):
                            s = input("  Season number [1]: ").strip()
                            result["season"] = int(s) if s.isdigit() else 1
                        # Re-search as TV
                        meta = tmdb_search_tv(manual_title)

            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta
                if meta.get("media_type"):
                    result["media_type"] = meta["media_type"]
            else:
                result["title"] = manual_title
    else:
        # Non-interactive, no TMDb match — use whatever we have
        result["title"] = cleaned or raw_label
        log.warning(f"  No TMDb match. Using label: {result['title']}")

    # Resolve "auto" to "movie" if dual search didn't pick a type
    if result["media_type"] == "auto":
        result["media_type"] = "movie"

    # Interactive confirmation
    if sys.stdin.isatty():
        type_str = result["media_type"].upper()
        season_str = f" S{result['season']:02d}" if result.get("season") else ""
        print(f"\n  Detected [{type_str}]: {result['title']}"
              + (f" ({result['year']})" if result.get('year') else "")
              + season_str)

        # If the user already manually specified the type (via manual prompt),
        # don't clutter the confirm with tv/movie switch options
        user_confirmed_type = result.get("_user_set_type", False)
        if user_confirmed_type:
            confirm = input("  Accept? [Y/n/edit]: ").strip().lower()
        else:
            confirm = input("  Accept? [Y/n/edit/tv/movie]: ").strip().lower()

        if confirm == 'n':
            result["title"] = input("  Enter correct title: ").strip()
            result["year"] = input("  Enter year (or Enter to skip): ").strip() or None
            mtype = input("  Type [m]ovie or [t]v? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
            else:
                result["media_type"] = "movie"
            # Re-search
            if result["media_type"] == "tv":
                meta = tmdb_search_tv(result["title"])
            else:
                meta = tmdb_search(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'tv' and not user_confirmed_type:
            result["media_type"] = "tv"
            meta = tmdb_search_tv(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'movie' and not user_confirmed_type:
            result["media_type"] = "movie"
            meta = tmdb_search(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'edit':
            result["title"] = input(f"  Title [{result['title']}]: ").strip() or result["title"]
            result["year"] = input(f"  Year [{result.get('year', '')}]: ").strip() or result.get("year")

        # For TV, get season/episode info
        if result["media_type"] == "tv":
            if not result.get("season"):
                s = input("  Season number: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
            if not result.get("start_episode"):
                ep = input("  Starting episode number [1]: ").strip()
                result["start_episode"] = int(ep) if ep.isdigit() else 1

    # Only cache results that have TMDb metadata (don't cache garbage labels)
    if result.get("metadata") and result.get("title"):
        cache[raw_label] = {
            "title": result["title"],
            "year": result.get("year"),
            "media_type": result["media_type"],
            "season": result.get("season"),
            "tmdb_id": result.get("metadata", {}).get("tmdb_id"),
        }
        save_metadata_cache(cache)

    return result


def download_artwork(metadata, dest_dir):
    """Download poster and fanart from TMDb to the output folder (for media servers)."""
    if not metadata:
        return

    poster_path = metadata.get("poster_path")
    if poster_path:
        url = f"https://image.tmdb.org/t/p/original{poster_path}"
        dest = Path(dest_dir) / "poster.jpg"
        try:
            urllib.request.urlretrieve(url, str(dest))
            log.info(f"  Poster saved: {dest}")
        except (urllib.error.URLError, OSError) as e:
            log.warning(f"  Could not download poster: {e}")

    # Also fetch backdrop/fanart if available
    backdrop = metadata.get("backdrop_path")
    if backdrop:
        url = f"https://image.tmdb.org/t/p/original{backdrop}"
        dest = Path(dest_dir) / "fanart.jpg"
        try:
            urllib.request.urlretrieve(url, str(dest))
            log.info(f"  Fanart saved: {dest}")
        except (urllib.error.URLError, OSError) as e:
            log.warning(f"  Could not download fanart: {e}")


def write_nfo(metadata, dest_dir, filename):
    """
    Write a Kodi/Jellyfin/Plex-compatible .nfo file with full metadata.
    This XML format is auto-read by most media servers for library enrichment.
    """
    if not metadata or not metadata.get("tmdb_id"):
        return

    title = metadata.get("title", "Unknown")
    year = metadata.get("year", "")
    nfo_path = Path(dest_dir) / f"{filename}.nfo"

    # Escape XML special chars
    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<movie>',
        f'  <title>{esc(title)}</title>',
        f'  <originaltitle>{esc(metadata.get("original_title", title))}</originaltitle>',
        f'  <year>{esc(year)}</year>',
        f'  <plot>{esc(metadata.get("overview", ""))}</plot>',
        f'  <tagline>{esc(metadata.get("tagline", ""))}</tagline>',
        f'  <runtime>{metadata.get("runtime", "")}</runtime>',
        f'  <mpaa>{esc(metadata.get("certification", ""))}</mpaa>',
        f'  <rating>{metadata.get("vote_average", "")}</rating>',
        f'  <studio>{esc(metadata.get("studio", ""))}</studio>',
        f'  <uniqueid type="tmdb">{metadata.get("tmdb_id", "")}</uniqueid>',
    ]

    # Genres
    for genre in metadata.get("genres", []):
        lines.append(f'  <genre>{esc(genre)}</genre>')

    # Directors
    for director in metadata.get("directors", []):
        lines.append(f'  <director>{esc(director)}</director>')

    # Cast
    for actor in metadata.get("cast", []):
        lines.append('  <actor>')
        lines.append(f'    <name>{esc(actor["name"])}</name>')
        lines.append(f'    <role>{esc(actor.get("role", ""))}</role>')
        lines.append('  </actor>')

    # Artwork references
    if metadata.get("poster_path"):
        lines.append('  <thumb aspect="poster">poster.jpg</thumb>')
    lines.append('  <fanart>')
    lines.append('    <thumb>fanart.jpg</thumb>')
    lines.append('  </fanart>')

    lines.append('</movie>')

    nfo_path.write_text('\n'.join(lines), encoding='utf-8')
    log.info(f"  NFO written: {nfo_path}")


def write_tv_nfo(metadata, dest_dir, show_title, season_num):
    """Write a tvshow.nfo for Kodi/Jellyfin at the show root level."""
    if not metadata or not metadata.get("tmdb_id"):
        return

    nfo_path = Path(dest_dir) / "tvshow.nfo"
    if nfo_path.exists():
        return  # Don't overwrite existing show NFO

    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<tvshow>',
        f'  <title>{esc(show_title)}</title>',
        f'  <originaltitle>{esc(metadata.get("original_title", show_title))}</originaltitle>',
        f'  <year>{esc(metadata.get("year", ""))}</year>',
        f'  <plot>{esc(metadata.get("overview", ""))}</plot>',
        f'  <mpaa>{esc(metadata.get("certification", ""))}</mpaa>',
        f'  <rating>{metadata.get("vote_average", "")}</rating>',
        f'  <studio>{esc(metadata.get("studio", ""))}</studio>',
        f'  <uniqueid type="tmdb">{metadata.get("tmdb_id", "")}</uniqueid>',
    ]

    for genre in metadata.get("genres", []):
        lines.append(f'  <genre>{esc(genre)}</genre>')
    for actor in metadata.get("cast", []):
        lines.append('  <actor>')
        lines.append(f'    <name>{esc(actor["name"])}</name>')
        lines.append(f'    <role>{esc(actor.get("role", ""))}</role>')
        lines.append('  </actor>')

    lines.append('</tvshow>')
    nfo_path.write_text('\n'.join(lines), encoding='utf-8')
    log.info(f"  Show NFO written: {nfo_path}")


def write_episode_nfo(episode_info, dest_path, show_title, season_num, episode_num):
    """Write a per-episode .nfo file."""
    nfo_path = dest_path.with_suffix('.nfo')

    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    ep_name = episode_info.get("name", f"Episode {episode_num}") if episode_info else f"Episode {episode_num}"
    ep_plot = episode_info.get("overview", "") if episode_info else ""
    ep_aired = episode_info.get("air_date", "") if episode_info else ""

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<episodedetails>',
        f'  <title>{esc(ep_name)}</title>',
        f'  <showtitle>{esc(show_title)}</showtitle>',
        f'  <season>{season_num}</season>',
        f'  <episode>{episode_num}</episode>',
        f'  <plot>{esc(ep_plot)}</plot>',
        f'  <aired>{esc(ep_aired)}</aired>',
        '</episodedetails>',
    ]
    nfo_path.write_text('\n'.join(lines), encoding='utf-8')


# ============================================================================
# TV SHOW: MULTI-EPISODE RIP
# ============================================================================

def rip_tv_disc(title_name, season_num, start_episode=1, episodes_info=None):
    """
    Rip all episode-length titles from a TV disc.
    TV discs typically have multiple titles of similar length (episodes)
    plus some shorter titles (extras, trailers).

    episodes_info: optional list of TMDb episode dicts for display names.
    Returns list of (mkv_path, episode_number) tuples.
    """
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_episode_length"]

    rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(f"{title_name}_S{season_num:02d}")
    rip_out.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("STEP 1: RIPPING TV DISC WITH MAKEMKV")
    log.info(f"  Show: {title_name} — Season {season_num}")
    log.info("=" * 60)

    # Scan disc
    log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "info", "disc:0"], timeout=120)
    if scan is None or scan.returncode != 0:
        log.error("Failed to scan disc.")
        return []

    # Detect source format for auto encoder tuning
    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

    # Parse titles
    titles = {}
    for line in scan.stdout.splitlines():
        if line.startswith("TINFO:"):
            match = re.match(r'TINFO:(\d+),(\d+),\d+,"?(.*?)"?$', line)
            if match:
                tid, attr_id, value = match.groups()
                tid = int(tid)
                attr_id = int(attr_id)
                if tid not in titles:
                    titles[tid] = {}
                titles[tid][attr_id] = value

    if not titles:
        log.error("No titles found on disc.")
        return []

    # Find episode-length titles (filter by duration)
    episode_candidates = []
    for tid, attrs in titles.items():
        duration_str = attrs.get(9, "0:00:00")
        try:
            parts = duration_str.split(":")
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            seconds = 0

        name = attrs.get(2, f"Title {tid}")
        size_bytes = int(attrs.get(11, 0))
        size_gb = size_bytes / (1024 ** 3) if size_bytes else 0

        log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

        if seconds >= min_length:
            episode_candidates.append((tid, seconds, size_bytes, name))

    if not episode_candidates:
        log.error(f"No titles found longer than {min_length // 60} minutes.")
        return []

    # Sort by title ID (usually sequential = episode order)
    episode_candidates.sort(key=lambda x: x[0])

    # Filter out the main "play all" title if present (usually longest by far)
    if len(episode_candidates) > 2:
        durations = [c[1] for c in episode_candidates]
        avg_duration = sum(durations) / len(durations)
        # If any title is >2x the average, it's probably a "play all" composite
        filtered = [c for c in episode_candidates if c[1] < avg_duration * 1.8]
        if len(filtered) >= 2:
            removed = len(episode_candidates) - len(filtered)
            if removed > 0:
                log.info(f"  Filtered out {removed} 'play all' title(s)")
            episode_candidates = filtered

    log.info(f"\n  Found {len(episode_candidates)} episodes to rip")

    # Build episode name lookup from TMDb data and show episode list
    _ep_names = {}
    if episodes_info:
        for ei in episodes_info:
            _ep_names[ei.get("episode_number")] = ei.get("name", "")
        log.info("  Episode names from TMDb:")
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_name = _ep_names.get(ep_num, "Unknown")
            log.info(f"    E{ep_num:02d}: {ep_name}")

    # Rip each episode
    ripped = []
    for idx, (tid, seconds, size_bytes, name) in enumerate(episode_candidates):
        if _shutdown_requested:
            log.info("  Stopping — Ctrl+C received. Partial rips preserved in _rips/.")
            break
        ep_num = start_episode + idx
        tmdb_name = _ep_names.get(ep_num, "")
        display = f"E{ep_num:02d}"
        if tmdb_name:
            display += f" — {tmdb_name}"
        log.info(f"\n  Ripping {display} (title {tid}, {seconds // 60} min)...")

        returncode, stderr = run_cmd_progress(
            [
                makemkv,
                "-r",
                "mkv",
                "disc:0",
                str(tid),
                str(rip_out),
                "--minlength=0",
                "--progress=-stdout",
            ],
            timeout=3600,
            progress_handler=makemkv_progress,
        )
        if not (tui and tui.enabled):
            print()  # Newline after progress

        if returncode is None or returncode != 0:
            log.error(f"  Failed to rip title {tid}")
            continue

        # Find the newly created MKV (MakeMKV names them title_tXX.mkv)
        mkv_files = sorted(rip_out.glob("*.mkv"), key=lambda f: f.stat().st_mtime)
        if mkv_files:
            latest = mkv_files[-1]
            size_gb = latest.stat().st_size / (1024 ** 3)
            log.info(f"  Ripped: {latest.name} ({size_gb:.1f} GB)")
            ripped.append((latest, ep_num))

    log.info(f"\n  Ripped {len(ripped)} episodes total")
    return ripped


def _rip_tv_disc_parallel(title_name, season_num, start_episode, episodes_info,
                           encode_queue, rip_dirs_to_clean):
    """
    Rip TV episodes and push each to encode_queue immediately after ripping.
    The encode worker thread picks them up and starts encoding in parallel
    with the next episode's rip (optical drive = I/O, HandBrake = CPU/GPU).

    Returns list of (mkv_path, ep_num) for tracking.
    """
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_episode_length"]

    rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(f"{title_name}_S{season_num:02d}")
    rip_out.mkdir(parents=True, exist_ok=True)
    rip_dirs_to_clean.add(rip_out)

    log.info("=" * 60)
    log.info("STEP 1: RIPPING TV DISC (parallel encode enabled)")
    log.info(f"  Show: {title_name} — Season {season_num}")
    log.info("=" * 60)

    # Scan disc
    log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "info", "disc:0"], timeout=120)
    if scan is None or scan.returncode != 0:
        log.error("Failed to scan disc.")
        return []

    # Detect source format for auto encoder tuning
    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

    # Parse titles
    titles = {}
    for line in scan.stdout.splitlines():
        if line.startswith("TINFO:"):
            match = re.match(r'TINFO:(\d+),(\d+),\d+,"?(.*?)"?$', line)
            if match:
                tid, attr_id, value = match.groups()
                tid = int(tid)
                attr_id = int(attr_id)
                if tid not in titles:
                    titles[tid] = {}
                titles[tid][attr_id] = value

    if not titles:
        log.error("No titles found on disc.")
        return []

    # Find episode-length titles
    episode_candidates = []
    for tid, attrs in titles.items():
        duration_str = attrs.get(9, "0:00:00")
        try:
            parts = duration_str.split(":")
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            seconds = 0

        name = attrs.get(2, f"Title {tid}")
        size_bytes = int(attrs.get(11, 0))
        size_gb = size_bytes / (1024 ** 3) if size_bytes else 0

        log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

        if seconds >= min_length:
            episode_candidates.append((tid, seconds, size_bytes, name))

    if not episode_candidates:
        log.error(f"No titles found longer than {min_length // 60} minutes.")
        return []

    # Sort by title ID
    episode_candidates.sort(key=lambda x: x[0])

    # Filter out "play all" title
    if len(episode_candidates) > 2:
        durations = [c[1] for c in episode_candidates]
        avg_duration = sum(durations) / len(durations)
        filtered = [c for c in episode_candidates if c[1] < avg_duration * 1.8]
        if len(filtered) >= 2:
            removed = len(episode_candidates) - len(filtered)
            if removed > 0:
                log.info(f"  Filtered out {removed} 'play all' title(s)")
            episode_candidates = filtered

    log.info(f"\n  Found {len(episode_candidates)} episodes to rip")

    # Build episode name lookup
    _ep_names = {}
    if episodes_info:
        for ei in episodes_info:
            _ep_names[ei.get("episode_number")] = ei.get("name", "")
        log.info("  Episode names from TMDb:")
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_name = _ep_names.get(ep_num, "Unknown")
            log.info(f"    E{ep_num:02d}: {ep_name}")

    log.info("\n  Parallel mode: encoding starts as soon as each episode is ripped\n")

    # Populate TUI episode queue
    if tui and tui.enabled:
        ep_list = []
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_list.append((ep_num, _ep_names.get(ep_num, "")))
        tui.set_episodes(ep_list)

    # Rip each episode — push to encode queue as soon as done
    ripped = []
    for idx, (tid, seconds, size_bytes, name) in enumerate(episode_candidates):
        if _shutdown_requested:
            log.info("  [RIP] Stopping — Ctrl+C received.")
            break
        ep_num = start_episode + idx
        tmdb_name = _ep_names.get(ep_num, "")
        display = f"E{ep_num:02d}"
        if tmdb_name:
            display += f" — {tmdb_name}"
        log.info(f"  [RIP] {display} (title {tid}, {seconds // 60} min)...")

        if tui and tui.enabled:
            tui.set_episode_status(ep_num, "ripping")
            tui.update_rip(0.0, task=display)

        returncode, stderr = run_cmd_progress(
            [
                makemkv,
                "-r",
                "mkv",
                "disc:0",
                str(tid),
                str(rip_out),
                "--minlength=0",
                "--progress=-stdout",
            ],
            timeout=3600,
            progress_handler=makemkv_progress,
        )
        if not (tui and tui.enabled):
            print()  # Newline after progress (non-TUI only)

        if returncode is None or returncode != 0:
            log.error(f"  [RIP] Failed title {tid}")
            if tui and tui.enabled:
                tui.set_episode_status(ep_num, "failed")
            continue

        if tui and tui.enabled:
            tui.set_episode_status(ep_num, "ripped")
            tui.update_rip(100.0)

        # Find the newly created MKV
        mkv_files = sorted(rip_out.glob("*.mkv"), key=lambda f: f.stat().st_mtime)
        if mkv_files:
            latest = mkv_files[-1]
            size_gb = latest.stat().st_size / (1024 ** 3)
            log.info(f"  [RIP] Done: {latest.name} ({size_gb:.1f} GB) → queued for encode")
            ripped.append((latest, ep_num))
            # Push to encode worker immediately
            encode_queue.put((latest, ep_num))

    log.info(f"\n  [RIP] Finished — {len(ripped)} episodes ripped")
    return ripped


def organize_tv_episode(encoded_file, show_title, year, season_num, episode_num, episode_name=""):
    """
    Move encoded TV episode to Plex/Jellyfin naming convention:
      TV Shows/Show Name (Year)/Season 01/Show Name - S01E01 - Episode Name.mkv
    """
    clean_title = sanitize_filename(show_title)
    if year:
        show_folder = f"{clean_title} ({year})"
    else:
        show_folder = clean_title

    season_folder = f"Season {season_num:02d}"
    ep_tag = f"S{season_num:02d}E{episode_num:02d}"

    if episode_name:
        ep_filename = f"{clean_title} - {ep_tag} - {sanitize_filename(episode_name)}.mkv"
    else:
        ep_filename = f"{clean_title} - {ep_tag}.mkv"

    dest_dir = Path(CONFIG["tv_encode_dir"]) / show_folder / season_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / ep_filename

    if encoded_file != dest_file:
        shutil.move(str(encoded_file), str(dest_file))

    log.info(f"  {ep_tag}: {dest_file.name}")
    return dest_file


# ============================================================================
# STEP 1: RIP WITH MAKEMKV
# ============================================================================

def rip_disc(title_name=None):
    """
    Rip the main feature from the inserted Blu-ray disc.
    Returns (mkv_path, source_info) tuple, or (None, None) on failure.
    source_info contains disc_type, resolution, interlaced for encoder tuning.
    """
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_title_length"]

    if title_name:
        rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(title_name)
    else:
        rip_out = Path(CONFIG["rip_dir"]) / f"rip_{datetime.now():%Y%m%d_%H%M%S}"

    rip_out.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("STEP 1: RIPPING DISC WITH MAKEMKV")
    log.info("=" * 60)

    # First, scan disc to find titles
    log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "info", "disc:0"], timeout=120)
    if scan is None or scan.returncode != 0:
        log.error("Failed to scan disc. Is a disc inserted?")
        return None, None

    # Detect source format (DVD/BD/UHD) for auto encoder tuning
    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

    # Parse title info to find the main feature
    titles = {}
    for line in scan.stdout.splitlines():
        # TINFO:title_id,attribute_id,code,value
        if line.startswith("TINFO:"):
            match = re.match(r'TINFO:(\d+),(\d+),\d+,"?(.*?)"?$', line)
            if match:
                tid, attr_id, value = match.groups()
                tid = int(tid)
                attr_id = int(attr_id)
                if tid not in titles:
                    titles[tid] = {}
                titles[tid][attr_id] = value

    if not titles:
        log.error("No titles found on disc.")
        return None, None

    # Find the longest title (attribute 9 = duration in seconds, 27 = file size)
    # Attribute 9 = duration string "H:MM:SS", attribute 11 = bytes
    best_title = None
    best_duration = 0

    for tid, attrs in titles.items():
        duration_str = attrs.get(9, "0:00:00")
        try:
            parts = duration_str.split(":")
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            seconds = 0

        if seconds > best_duration:
            best_duration = seconds
            best_title = tid

        # Attribute 2 = name
        name = attrs.get(2, f"Title {tid}")
        size_bytes = int(attrs.get(11, 0))
        size_gb = size_bytes / (1024 ** 3) if size_bytes else 0
        log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

    if best_title is None or best_duration < min_length:
        log.error(f"No title found longer than {min_length // 60} minutes.")
        return None, None

    log.info(f"Selected title {best_title} ({best_duration // 60} min)")

    # Rip the selected title (with live progress)
    log.info(f"Ripping to: {rip_out}")
    start_time = time.time()
    returncode, stderr = run_cmd_progress(
        [
            makemkv,
            "-r",
            "mkv",
            "disc:0",
            str(best_title),
            str(rip_out),
            "--minlength=0",      # We already filtered
            "--progress=-stdout",
        ],
        timeout=7200,
        progress_handler=makemkv_progress,
    )
    if not (tui and tui.enabled):
        print()  # Newline after progress

    rip_elapsed = time.time() - start_time
    rip_min = int(rip_elapsed // 60)
    log.info(f"Rip completed in {rip_min} minutes.")

    if returncode is None or returncode != 0:
        log.error("MakeMKV rip failed!")
        if stderr:
            log.error(stderr[-500:])
        return None, None

    # Find the output MKV
    mkv_files = list(rip_out.glob("*.mkv"))
    if not mkv_files:
        log.error("No MKV file found after rip!")
        return None, None

    # Use the largest MKV (in case multiple were created)
    mkv_file = max(mkv_files, key=lambda f: f.stat().st_size)
    size_gb = mkv_file.stat().st_size / (1024 ** 3)
    log.info(f"Rip complete: {mkv_file.name} ({size_gb:.1f} GB)")

    return mkv_file, source_info


# ============================================================================
# STEP 2: COMPRESS WITH HANDBRAKE (HDR10/DV PASSTHROUGH)
# ============================================================================

def compress_mkv(input_mkv, title_name=None, output_dir=None):
    """
    Compress with HandBrake, preserving HDR10 and Dolby Vision metadata.
    Tuned for quality viewing on LG C1 OLED and Dell 5K2K HDR.
    output_dir: override where the encoded file is placed (default: encode_dir).
    """
    hb = CONFIG["handbrake_bin"]

    if title_name:
        out_name = sanitize_filename(title_name)
    else:
        out_name = input_mkv.stem

    dest = Path(output_dir) if output_dir else Path(CONFIG["encode_dir"])
    dest.mkdir(parents=True, exist_ok=True)
    output_file = dest / f"{out_name}.mkv"

    # Don't overwrite existing encodes
    if output_file.exists():
        counter = 1
        while output_file.exists():
            output_file = dest / f"{out_name} ({counter}).mkv"
            counter += 1

    is_hq = CONFIG.get("hq_mode", False)
    mode_label = "SOFTWARE x265 (HQ)" if is_hq else "VIDEOTOOLBOX (HW)"

    log.info("=" * 60)
    log.info("STEP 2: COMPRESSING WITH HANDBRAKE")
    log.info("=" * 60)
    log.info(f"  Input:   {input_mkv}")
    log.info(f"  Output:  {output_file}")
    log.info(f"  Mode:    {mode_label}")
    log.info(f"  Encoder: {CONFIG['encoder']} @ RF {CONFIG['quality_rf']}")
    log.info(f"  Preset:  {CONFIG['encoder_preset']}")
    log.info("")
    if is_hq:
        log.info("  HQ mode: software x265 — slower but best quality.")
        log.info("  This will take a while for 4K. Go grab a coffee (or three).")
    else:
        log.info("  HW mode: VideoToolbox — fast encoding via Apple Silicon.")
    log.info("")

    cmd = [
        hb,
        "--input", str(input_mkv),
        "--output", str(output_file),
        "--format", "av_mkv",

        # Video encoder
        "--encoder", CONFIG["encoder"],
        "--quality", str(CONFIG["quality_rf"]),
        "--encoder-preset", CONFIG["encoder_preset"],
        "--encoder-profile", CONFIG["encoder_profile"],
        "--encoder-level", CONFIG["encoder_level"],
        "--pfr",
    ]

    # Encoder-specific options
    if is_hq:
        # Software x265: extra psychovisual tuning for best quality
        cmd.extend([
            "--encopts",
            "aq-mode=3:rd=4:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=60:bframes=8:ref=5",
        ])
    # VT doesn't support --encopts, so skip for hardware mode

    cmd.extend([
        # Keep original resolution, no auto-crop
        "--non-anamorphic",
        "--crop", "0:0:0:0",

        # Audio: keep ALL tracks
        "--all-audio",
        "--audio-fallback", "aac",

        # Subtitles: keep all
        "--all-subtitles",

        # HDR: passthrough (HandBrake auto-detects HDR10/DV metadata)
    ])

    # Audio encoding based on viewing profile
    audio_mode = CONFIG.get("audio_mode", "copy,aac")
    if audio_mode == "aac":
        cmd.extend(["--aencoder", "aac", "--mixdown", "stereo"])
    else:
        cmd.extend(["--aencoder", "copy,aac", "--mixdown", "none,stereo"])

    # Deinterlace (auto-enabled for DVDs)
    if CONFIG.get("deinterlace"):
        cmd.extend(["--comb-detect", "--decomb"])
        log.info("  Deinterlace: enabled (comb-detect + decomb)")

    # Optional grain tune (software x265 only — VT doesn't support tune)
    if CONFIG["encoder_tune"] and is_hq:
        cmd.extend(["--encoder-tune", CONFIG["encoder_tune"]])

    start_time = time.time()
    returncode, stderr = run_cmd_progress(
        cmd, timeout=86400, progress_handler=handbrake_progress
    )
    if not (tui and tui.enabled):
        print()  # Newline after progress

    if returncode is None or returncode != 0:
        log.error("HandBrake encoding failed!")
        if stderr:
            log.error(stderr[-1000:])
        return None

    if not output_file.exists():
        log.error(f"HandBrake exited OK but output file missing: {output_file}")
        if stderr:
            log.error(stderr[-1000:])
        return None

    elapsed = time.time() - start_time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)

    in_size = input_mkv.stat().st_size / (1024 ** 3)
    out_size = output_file.stat().st_size / (1024 ** 3)
    ratio = (1 - out_size / in_size) * 100

    log.info(f"Encode complete in {hours}h {minutes}m {seconds}s")
    log.info(f"  Source:  {in_size:.1f} GB")
    log.info(f"  Output:  {out_size:.1f} GB")
    log.info(f"  Savings: {ratio:.0f}%")

    return output_file


# ============================================================================
# STEP 3: ORGANIZE (PLEX/JELLYFIN-COMPATIBLE NAMING)
# ============================================================================

def organize_file(encoded_file, title_name, year=None):
    """
    Rename and move the encoded file to Plex/Jellyfin naming convention:
      Movies/Title (Year)/Title (Year).mkv
    """
    log.info("=" * 60)
    log.info("STEP 3: ORGANIZING")
    log.info("=" * 60)

    clean_title = sanitize_filename(title_name)
    if year:
        folder_name = f"{clean_title} ({year})"
    else:
        folder_name = clean_title

    dest_dir = Path(CONFIG["encode_dir"]) / folder_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{folder_name}.mkv"

    if encoded_file != dest_file:
        shutil.move(str(encoded_file), str(dest_file))

    log.info(f"  Final location: {dest_file}")
    return dest_file


# ============================================================================
# JELLYFIN INTEGRATION
# ============================================================================

def jellyfin_scan_library(quiet=False):
    """Trigger a Jellyfin library scan so new content appears immediately."""
    api_key = CONFIG["jellyfin_api_key"]
    base_url = CONFIG["jellyfin_url"]

    if not api_key:
        if not quiet:
            log.info("No Jellyfin API key configured — skipping library scan.")
            log.info("Set JELLYFIN_API_KEY to enable auto-scan after rips.")
        return False

    url = f"{base_url}/Library/Refresh"
    if not quiet:
        log.info("Triggering Jellyfin library scan...")

    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f'MediaBrowser Token="{api_key}"',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204 or resp.status == 200:
                if quiet:
                    log.info("  [ENCODE] Jellyfin scan triggered")
                else:
                    log.info("  Jellyfin library scan triggered")
                return True
    except (urllib.error.URLError, OSError) as e:
        if not quiet:
            log.warning(f"  Jellyfin scan failed: {e}")
            log.warning("  Is Jellyfin running? Check http://localhost:8096")
        return False

    return False


def jellyfin_check_status():
    """Check if Jellyfin is running and reachable."""
    base_url = CONFIG["jellyfin_url"]
    try:
        req = urllib.request.Request(f"{base_url}/System/Info/Public")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            version = data.get("Version", "unknown")
            server_name = data.get("ServerName", "unknown")
            log.info(f"  Jellyfin {version} ({server_name}) is running")
            return True
    except (urllib.error.URLError, OSError):
        return False


# ============================================================================
# CLEANUP
# ============================================================================

def hash_file(filepath, algorithm="md5"):
    """Compute hash of a file. Uses MD5 by default (fast, good enough for integrity)."""
    h = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)  # 8MB chunks
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_rip_manifest(rip_dir, files_info):
    """
    Write a manifest file alongside rips for integrity verification.
    files_info: list of (filepath, episode_num_or_None) dicts.
    """
    manifest_path = Path(rip_dir) / "rip_manifest.json"
    entries = []
    for fpath, ep_num in files_info:
        fpath = Path(fpath)
        if not fpath.exists():
            continue
        log.info(f"  Hashing {fpath.name}...")
        h = hash_file(fpath)
        entries.append({
            "filename": fpath.name,
            "size_bytes": fpath.stat().st_size,
            "md5": h,
            "episode": ep_num,
            "ripped_at": datetime.now().isoformat(),
        })

    manifest = {
        "rip_dir": str(rip_dir),
        "created": datetime.now().isoformat(),
        "files": entries,
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info(f"  Manifest written: {manifest_path.name} ({len(entries)} files)")
    return manifest_path


def verify_rip_manifest(rip_dir):
    """
    Verify ripped files against their manifest. Returns list of valid (filepath, ep_num) tuples.
    """
    manifest_path = Path(rip_dir) / "rip_manifest.json"
    if not manifest_path.exists():
        log.warning(f"  No manifest found in {rip_dir}")
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    valid = []
    for entry in manifest.get("files", []):
        fpath = Path(rip_dir) / entry["filename"]
        if not fpath.exists():
            log.warning(f"  Missing: {entry['filename']}")
            continue
        if fpath.stat().st_size != entry["size_bytes"]:
            log.warning(f"  Size mismatch: {entry['filename']}")
            continue
        # Quick size check is enough for re-encode; full hash verify with --verify-rips
        valid.append((fpath, entry.get("episode")))
        log.info(f"  Verified: {entry['filename']} ({entry['size_bytes'] / (1024**3):.1f} GB)")

    return valid


def cleanup_rips(rip_dir=None):
    """
    Remove raw rips. If rip_dir given, clean that. Otherwise clean all of _rips/.
    """
    if rip_dir:
        rip_dir = Path(rip_dir)
        if rip_dir.exists():
            size_gb = sum(f.stat().st_size for f in rip_dir.rglob("*") if f.is_file()) / (1024 ** 3)
            log.info(f"Cleaning up {rip_dir.name} ({size_gb:.1f} GB)...")
            shutil.rmtree(rip_dir, ignore_errors=True)
            log.info("  Done.")
    else:
        rips_base = Path(CONFIG["rip_dir"])
        if not rips_base.exists():
            log.info("No rips directory found.")
            return
        total = 0
        for d in sorted(rips_base.iterdir()):
            if d.is_dir():
                size_gb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / (1024 ** 3)
                log.info(f"  {d.name}: {size_gb:.1f} GB")
                total += size_gb
        if total == 0:
            log.info("  No rips to clean up.")
            return
        log.info(f"\n  Total: {total:.1f} GB")
        if sys.stdin.isatty():
            confirm = input("  Delete all rips? [y/N]: ").strip().lower()
            if confirm != 'y':
                log.info("  Skipped.")
                return
        for d in rips_base.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        log.info("  All rips cleaned up.")


# ============================================================================
# FULL PIPELINE
# ============================================================================

def run_pipeline(title_name=None, year=None, media_type=None,
                 season=None, start_episode=None):
    """Run the complete rip → compress → organize pipeline for movies or TV."""
    ensure_dirs()

    if not check_external_drive():
        return False

    drive = check_drive()
    if drive and not drive["found"]:
        return False

    # Auto-detect title and metadata from disc + TMDb
    disc_info = get_disc_metadata(
        manual_title=title_name,
        manual_year=year,
        media_type=media_type,
        season=season,
        start_episode=start_episode,
    )

    title_name = disc_info["title"]
    year = disc_info.get("year")
    media_type = disc_info.get("media_type", "movie")
    meta = disc_info.get("metadata")

    if _shutdown_requested:
        log.info("Pipeline cancelled.")
        return False

    if not title_name:
        log.error("No title could be determined. Use --title to specify manually.")
        return False

    log.info(f"\nStarting pipeline for [{media_type.upper()}]: {title_name}"
             + (f" ({year})" if year else ""))
    log.info(f"Start time: {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    # Start TUI if available and terminal supports it
    global tui
    if sys.stdout.isatty():
        tui = RipperTUI()
        tui.set_metadata(title_name, year, media_type)

        # Fetch and set poster art before starting the live display
        poster_path = meta.get("poster_path") if meta else None
        if poster_path:
            art = poster_to_ascii(poster_path)
            if art:
                tui.set_poster(art)

        tui.start()

    try:
        return _run_pipeline_inner(
            title_name, year, media_type, meta, disc_info,
        )
    finally:
        if tui:
            tui.stop()
            tui = None


def _run_pipeline_inner(title_name, year, media_type, meta, disc_info):
    """Inner pipeline logic, wrapped by run_pipeline for TUI lifecycle."""

    # ---- MOVIE PIPELINE ----
    if media_type == "movie":
        if tui and tui.enabled:
            tui.update_rip(0.0, task="Main feature")
        rip_mkv, source_info = rip_disc(title_name)
        if _shutdown_requested:
            log.info("Pipeline cancelled during rip. Raw files preserved in _rips/.")
            return False
        if not rip_mkv:
            log.error("Pipeline failed at rip stage.")
            return False
        if tui and tui.enabled:
            tui.update_rip(100.0)
            tui.log(f"Rip complete: {rip_mkv.name}")

        # Write manifest for the rip (enables --reencode later)
        write_rip_manifest(rip_mkv.parent, [(rip_mkv, None)])

        if tui and tui.enabled:
            tui.update_encode(0.0, task=title_name)
        encoded = compress_mkv(rip_mkv, title_name)
        if not encoded:
            log.error("Pipeline failed at encode stage. Raw rip preserved in _rips/.")
            return False
        if tui and tui.enabled:
            tui.update_encode(100.0)
            tui.log(f"Encode complete: {encoded.name}")

        final = organize_file(encoded, title_name, year)

        # Write metadata
        final_dir = final.parent
        if meta:
            folder_name = sanitize_filename(
                f"{title_name} ({year})" if year else title_name
            )
            write_nfo(meta, final_dir, folder_name)
            download_artwork(meta, final_dir)

        # Rips preserved — use --cleanup to free space
        rip_size = rip_mkv.stat().st_size / (1024 ** 3) if rip_mkv.exists() else 0
        log.info(f"Raw rip preserved in _rips/ ({rip_size:.1f} GB). Use --cleanup to remove.")

    # ---- TV SHOW PIPELINE (parallel rip + encode) ----
    else:
        season_num = disc_info.get("season") or 1
        ep_start = disc_info.get("start_episode") or 1

        # Fetch episode names from TMDb
        episodes_info = []
        if meta and meta.get("tmdb_id"):
            episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], season_num)

        # Build episode name lookup
        ep_name_map = {}
        ep_info_map = {}
        for ei in episodes_info:
            ep_name_map[ei.get("episode_number")] = ei.get("name", "")
            ep_info_map[ei.get("episode_number")] = ei

        # Queue for passing ripped episodes to the encode worker
        encode_queue = queue.Queue()
        encode_results = []  # (ep_num, final_path) filled by worker
        rip_dirs_to_clean = set()

        def _encode_worker():
            """Background thread: encodes episodes as they arrive in the queue."""
            while True:
                item = encode_queue.get()
                if item is None:  # Sentinel = done
                    break
                if _shutdown_requested:
                    encode_queue.task_done()
                    continue

                rip_mkv, ep_num = item
                ep_name = ep_name_map.get(ep_num, "")
                ep_info = ep_info_map.get(ep_num)
                ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                encode_name = f"{sanitize_filename(title_name)} - {ep_tag}"

                log.info(f"\n  [ENCODE] {ep_tag}: {ep_name or 'Episode ' + str(ep_num)}")

                if tui and tui.enabled:
                    tui.set_episode_status(ep_num, "encoding")
                    tui.update_encode(0.0, task=f"{ep_tag} — {ep_name}" if ep_name else ep_tag)

                encoded = compress_mkv(rip_mkv, encode_name,
                                       output_dir=CONFIG["rip_dir"])
                if not encoded:
                    log.error(f"  [ENCODE] Failed {ep_tag}")
                    if tui and tui.enabled:
                        tui.set_episode_status(ep_num, "failed")
                    encode_queue.task_done()
                    continue

                # Organize into TV folder structure
                final = organize_tv_episode(
                    encoded, title_name, year, season_num, ep_num, ep_name
                )

                # Write per-episode NFO
                write_episode_nfo(ep_info, final, title_name, season_num, ep_num)

                encode_results.append((ep_num, final))

                if tui and tui.enabled:
                    tui.set_episode_status(ep_num, "done")
                    tui.update_encode(100.0)
                    tui.log(f"{ep_tag} complete: {final.name}")

                # Trigger Jellyfin scan so this episode appears immediately
                jellyfin_scan_library(quiet=True)

                encode_queue.task_done()

        # Start the encode worker thread
        encoder_thread = threading.Thread(target=_encode_worker, daemon=True)
        encoder_thread.start()

        # Rip episodes — each one is queued for encoding as soon as it finishes
        ripped_episodes = _rip_tv_disc_parallel(
            title_name, season_num, ep_start, episodes_info,
            encode_queue, rip_dirs_to_clean,
        )

        if not ripped_episodes and not encode_results:
            log.error("Pipeline failed at rip stage — no episodes ripped.")
            encode_queue.put(None)  # Stop worker
            encoder_thread.join(timeout=5)
            return False

        # Signal encoder that no more episodes are coming, then wait
        encode_queue.put(None)
        log.info("\n  Waiting for final encode to finish...")
        encoder_thread.join()

        # Write show-level NFO and artwork
        if meta:
            show_dir = Path(CONFIG["tv_encode_dir"]) / sanitize_filename(
                f"{title_name} ({year})" if year else title_name
            )
            show_dir.mkdir(parents=True, exist_ok=True)
            write_tv_nfo(meta, show_dir, title_name, season_num)
            download_artwork(meta, show_dir)

        # Write manifests for all rip directories (enables --reencode later)
        for rip_dir in rip_dirs_to_clean:
            if rip_dir.exists():
                mkv_files = [(f, None) for f in sorted(rip_dir.glob("*.mkv"))]
                if mkv_files:
                    # Attach episode numbers from ripped_episodes
                    ep_map = {str(rip.resolve()): ep for rip, ep in ripped_episodes}
                    manifest_files = []
                    for f, _ in mkv_files:
                        ep = ep_map.get(str(f.resolve()))
                        manifest_files.append((f, ep))
                    write_rip_manifest(rip_dir, manifest_files)
                rip_size = sum(f.stat().st_size for f in rip_dir.rglob("*") if f.is_file()) / (1024 ** 3)
                log.info(f"Raw rips preserved in _rips/ ({rip_size:.1f} GB). Use --cleanup to remove.")

        total_done = len(encode_results)
        final = f"{total_done} episodes"

    # Notify Jellyfin
    jellyfin_scan_library()

    # Eject disc
    eject_disc()

    log.info("")
    log.info("=" * 60)
    log.info("PIPELINE COMPLETE!")
    log.info(f"  Result: {final}")
    if meta:
        genres = ', '.join(meta.get('genres', []))
        creators = ', '.join(meta.get('directors', []))
        log.info(f"  Genres: {genres}")
        if creators:
            log.info(f"  {'Created by' if media_type == 'tv' else 'Director(s)'}: {creators}")
    log.info(f"  Time: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info("=" * 60)

    # macOS notification
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "Finished: {title_name}" '
            f'with title "Blu-ray Pipeline" sound name "Glass"',
        ])
    except Exception:
        pass

    return True


# ============================================================================
# WATCH MODE — Auto-detect disc insertion
# ============================================================================

def watch_mode():
    """Poll for disc insertion and automatically start the pipeline."""
    log.info("Watch mode active. Waiting for disc insertion...")
    log.info(f"Polling every {CONFIG['poll_interval']} seconds.")
    log.info("Press Ctrl+C to stop.\n")

    was_inserted = disc_is_inserted()

    while True:
        try:
            is_inserted = disc_is_inserted()

            if is_inserted and not was_inserted:
                log.info("Disc detected! Starting pipeline...")
                time.sleep(5)  # Give the drive a moment to spin up
                run_pipeline()

            was_inserted = is_inserted
            time.sleep(CONFIG["poll_interval"])

        except KeyboardInterrupt:
            log.info("\nWatch mode stopped.")
            break


# ============================================================================
# CLI
# ============================================================================

def main():
    global log

    # Pre-parse just --init, --config, --show-config before full setup
    # (these need to run before logging, which needs CONFIG)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--init", action="store_true")
    pre.add_argument("--config", metavar="PATH")
    pre.add_argument("--show-config", action="store_true")
    pre_args, _ = pre.parse_known_args()

    # Load config file (before anything else)
    config_file = Path(pre_args.config) if pre_args.config else CONFIG_PATH
    load_config(pre_args.config)

    if pre_args.init:
        run_setup()
        return

    # First run — no config file exists, run setup automatically
    if not config_file.exists() and sys.stdin.isatty():
        run_setup(first_run=True)
        # Reload after setup
        load_config(pre_args.config)

    if pre_args.show_config:
        print(json.dumps(
            {k: CONFIG[k] for k in sorted(_CONFIGURABLE_KEYS)
             if k in CONFIG and CONFIG[k] is not None},
            indent=2,
        ))
        print(f"\nConfig file: {config_file}")
        return

    log = setup_logging()

    parser = argparse.ArgumentParser(
        description="4K Blu-ray Rip → Compress → Organize Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  uv run ripper.py                        Auto-detect and rip inserted disc
  uv run ripper.py --title "The Matrix"   Rip with a specific title
  uv run ripper.py --tv --season 2        Rip a TV disc (season 2)
  uv run ripper.py --hq                   Use software x265 (slower, best quality)
  uv run ripper.py --status               Check drive, Jellyfin, API keys
  uv run ripper.py --reencode _rips/dir   Re-encode from preserved rips
  uv run ripper.py --cleanup              Free space by deleting old rips
  uv run ripper.py --init                 Reconfigure settings

config:
  ~/.config/ripper/config.json (created on first run)
  Edit directly, or run --init to reconfigure.
  Env vars TMDB_API_KEY and JELLYFIN_API_KEY override the config file.
""",
    )

    # Setup & config
    parser.add_argument(
        "--init", action="store_true",
        help="Run the first-time setup wizard (creates config file)",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to config file (default: ~/.config/ripper/config.json)",
    )
    parser.add_argument(
        "--show-config", action="store_true",
        help="Print current configuration and exit",
    )

    parser.add_argument("--title", "-t", help="Movie/show title")
    parser.add_argument("--year", "-y", help="Release year")

    # Media type
    parser.add_argument(
        "--tv", action="store_true",
        help="Force TV show mode (auto-detected from disc label if possible)",
    )
    parser.add_argument(
        "--season", "-s", type=int,
        help="Season number (for TV shows)",
    )
    parser.add_argument(
        "--episode", "-e", type=int, default=1,
        help="Starting episode number on this disc (default: 1)",
    )

    # Operation modes
    parser.add_argument(
        "--check-drive", action="store_true",
        help="Only check drive compatibility, don't rip",
    )
    parser.add_argument(
        "--watch", "-w", action="store_true",
        help="Watch for disc insertion and auto-rip",
    )
    parser.add_argument(
        "--rip-only", action="store_true",
        help="Only rip, don't compress",
    )
    parser.add_argument(
        "--compress-only", metavar="MKV_PATH",
        help="Only compress an existing MKV file (skip rip)",
    )

    # Quality settings
    parser.add_argument(
        "--hq", action="store_true",
        help="Use software x265 encoder (slower, best quality) instead of VideoToolbox",
    )
    parser.add_argument(
        "--grain", action="store_true",
        help="Use x265 'grain' tune (for grainy/filmic sources, implies --hq)",
    )
    parser.add_argument(
        "--quality", "-q", type=int,
        help=f"Override RF quality value (default: {CONFIG['quality_rf']})",
    )

    # Utility commands
    parser.add_argument(
        "--status", action="store_true",
        help="Check status of drive, Jellyfin, and external storage",
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Trigger a Jellyfin library scan manually",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="Clear the metadata cache (useful after a bad lookup)",
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help="Interactively delete preserved raw rips in _rips/ to free space",
    )
    parser.add_argument(
        "--reencode", metavar="RIP_DIR",
        help="Re-encode from existing rips (path to a rip directory in _rips/)",
    )
    parser.add_argument(
        "--verify-rips", metavar="RIP_DIR",
        help="Verify rip integrity using MD5 hashes from manifest",
    )

    args = parser.parse_args()

    # Apply overrides
    if args.hq or args.grain:
        # Switch to software x265 mode (user override — auto_tune won't change this)
        CONFIG["hq_mode"] = True
        CONFIG["_user_set_hq"] = True
        CONFIG["encoder"] = "x265_10bit"
        CONFIG["quality_rf"] = 18
        CONFIG["encoder_preset"] = "slow"
        CONFIG["encoder_level"] = "5.1"
    if args.grain:
        CONFIG["encoder_tune"] = "grain"
    if args.quality:
        CONFIG["quality_rf"] = args.quality
        CONFIG["_user_set_quality"] = True

    log.info("4K Blu-ray Pipeline")
    log.info(f"Output drive: {CONFIG['output_base']}")
    log.info("")

    if args.clear_cache:
        cache_path = Path(CONFIG["metadata_cache"])
        if cache_path.exists():
            cache_path.unlink()
            log.info("Metadata cache cleared.")
        else:
            log.info("No cache file found.")
        return

    if args.cleanup:
        cleanup_rips()
        return

    if args.verify_rips:
        rip_dir = Path(args.verify_rips)
        if not rip_dir.exists():
            log.error(f"Directory not found: {rip_dir}")
            return
        log.info(f"Verifying rips in {rip_dir}...")
        manifest_path = rip_dir / "rip_manifest.json"
        if not manifest_path.exists():
            log.error("No manifest found. Cannot verify.")
            return
        with open(manifest_path) as f:
            manifest = json.load(f)
        all_ok = True
        for entry in manifest.get("files", []):
            fpath = rip_dir / entry["filename"]
            if not fpath.exists():
                log.error(f"  MISSING: {entry['filename']}")
                all_ok = False
                continue
            if fpath.stat().st_size != entry["size_bytes"]:
                log.error(f"  SIZE MISMATCH: {entry['filename']}")
                all_ok = False
                continue
            log.info(f"  Verifying MD5: {entry['filename']}...")
            actual_hash = hash_file(fpath)
            if actual_hash != entry["md5"]:
                log.error(f"  HASH MISMATCH: {entry['filename']} (expected {entry['md5']}, got {actual_hash})")
                all_ok = False
            else:
                log.info(f"  OK: {entry['filename']}")
        if all_ok:
            log.info("All files verified OK.")
        else:
            log.error("Some files failed verification!")
        return

    if args.reencode:
        rip_dir = Path(args.reencode)
        if not rip_dir.exists():
            log.error(f"Directory not found: {rip_dir}")
            return
        valid = verify_rip_manifest(rip_dir)
        if valid is None:
            # No manifest — just find all MKVs
            mkv_files = sorted(rip_dir.glob("*.mkv"))
            if not mkv_files:
                log.error(f"No MKV files found in {rip_dir}")
                return
            log.info(f"No manifest found. Found {len(mkv_files)} MKV file(s).")
            valid = [(f, None) for f in mkv_files]
        elif not valid:
            log.error("No valid rips found.")
            return

        log.info(f"Re-encoding {len(valid)} file(s) from {rip_dir}...")

        # Detect source format from the first file for encoder tuning
        # (can't scan disc — it may not be inserted)
        first_mkv = valid[0][0]
        log.info(f"  Probing {first_mkv.name} for source format...")

        # Need metadata — check cache or prompt
        disc_info = get_disc_metadata()
        title_name = disc_info["title"]
        year = disc_info.get("year")
        media_type = disc_info.get("media_type", "movie")
        meta = disc_info.get("metadata")

        if media_type == "movie" and len(valid) == 1:
            mkv_path = valid[0][0]
            encoded = compress_mkv(mkv_path, title_name)
            if encoded:
                final = organize_file(encoded, title_name, year)
                if meta:
                    folder_name = sanitize_filename(
                        f"{title_name} ({year})" if year else title_name
                    )
                    write_nfo(meta, final.parent, folder_name)
                    download_artwork(meta, final.parent)
                jellyfin_scan_library()
                log.info(f"Re-encode complete: {final}")
        else:
            # TV re-encode
            season_num = disc_info.get("season") or 1
            ep_start = disc_info.get("start_episode") or 1
            episodes_info = []
            if meta and meta.get("tmdb_id"):
                episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], season_num)

            for idx, (mkv_path, ep_num) in enumerate(valid):
                if _shutdown_requested:
                    break
                if ep_num is None:
                    ep_num = ep_start + idx
                ep_name = ""
                ep_info = None
                for ei in episodes_info:
                    if ei.get("episode_number") == ep_num:
                        ep_info = ei
                        ep_name = ei.get("name", "")
                        break
                ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                encode_name = f"{sanitize_filename(title_name)} - {ep_tag}"
                log.info(f"\n  Re-encoding {ep_tag}: {ep_name or 'Episode ' + str(ep_num)}")
                encoded = compress_mkv(mkv_path, encode_name, output_dir=CONFIG["rip_dir"])
                if not encoded:
                    log.error(f"  Failed to encode {ep_tag}")
                    continue
                final = organize_tv_episode(encoded, title_name, year, season_num, ep_num, ep_name)
                write_episode_nfo(ep_info, final, title_name, season_num, ep_num)
                jellyfin_scan_library(quiet=True)

            if meta:
                show_dir = Path(CONFIG["tv_encode_dir"]) / sanitize_filename(
                    f"{title_name} ({year})" if year else title_name
                )
                show_dir.mkdir(parents=True, exist_ok=True)
                write_tv_nfo(meta, show_dir, title_name, season_num)
                download_artwork(meta, show_dir)
            jellyfin_scan_library()
            log.info("Re-encode complete.")
        return

    if args.status:
        log.info("=" * 60)
        log.info("SYSTEM STATUS")
        log.info("=" * 60)
        check_external_drive()
        validate_api_keys()
        check_drive()
        jf_ok = jellyfin_check_status()
        if not jf_ok:
            log.warning("  Jellyfin: ❌ Not reachable at " + CONFIG["jellyfin_url"])
        if CONFIG["jellyfin_api_key"]:
            log.info("  Jellyfin API: ✅ Key configured")
        else:
            log.warning("  Jellyfin API: ❌ No key set (export JELLYFIN_API_KEY)")
        # Count existing movies and shows
        movies_dir = Path(CONFIG["encode_dir"])
        tv_dir = Path(CONFIG["tv_encode_dir"])
        if movies_dir.exists():
            movie_count = sum(1 for d in movies_dir.iterdir() if d.is_dir())
            log.info(f"  Movies: {movie_count} in {movies_dir}")
        if tv_dir.exists():
            show_count = sum(1 for d in tv_dir.iterdir() if d.is_dir())
            log.info(f"  TV Shows: {show_count} in {tv_dir}")
        return

    if args.scan:
        jellyfin_scan_library()
        return

    if args.check_drive:
        check_drive()
        return

    if args.watch:
        if not check_external_drive():
            return
        watch_mode()
        return

    if args.compress_only:
        ensure_dirs()
        mkv_path = Path(args.compress_only)
        if not mkv_path.exists():
            log.error(f"File not found: {mkv_path}")
            return
        title = args.title or mkv_path.stem
        encoded = compress_mkv(mkv_path, title)
        if encoded:
            organize_file(encoded, title, args.year)
        return

    if args.rip_only:
        ensure_dirs()
        if not check_external_drive():
            return
        rip_disc(args.title)
        return

    # Full pipeline — validate keys before starting
    if not validate_api_keys():
        log.error("Fix the above errors and try again.")
        return

    media_type = "tv" if args.tv else None
    run_pipeline(
        title_name=args.title,
        year=args.year,
        media_type=media_type,
        season=args.season,
        start_episode=args.episode,
    )


if __name__ == "__main__":
    main()
