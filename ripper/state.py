"""Shared mutable state for the ripper package.

Every module imports `from ripper import state` and accesses globals as
`state.CONFIG`, `state.tui`, etc.  This module imports nothing from the
ripper package, so it can never create circular imports.
"""

import threading

from rich.console import Console

# Global console instance
console = Console()

# Global CONFIG dict — modified in place throughout the codebase
CONFIG = {
    # Output paths — None means "not configured, run setup"
    "output_base": None,
    "rip_dir": None,
    "encode_dir": None,
    "tv_encode_dir": None,
    "log_dir": None,
    "metadata_cache": None,

    # MakeMKV
    "makemkv_bin": None,
    "min_title_length": 1200,
    "min_episode_length": 600,

    # HandBrake — VideoToolbox hardware encoding by default
    "handbrake_bin": None,
    "encoder": "vt_h265_10bit",
    "quality_rf": 55,
    "encoder_preset": "quality",
    "encoder_tune": None,
    "encoder_profile": "main10",
    "encoder_level": "auto",
    "hq_mode": False,
    "deinterlace": False,
    "output_format": "mkv",

    # Auto-optimized encoding — set by viewing profile
    "viewing_profile": "mixed",
    "quality_rf_dvd": 60,
    "quality_rf_bd": 52,
    "quality_rf_uhd": 50,
    "audio_mode": "copy,aac",
    "audio_lang": "eng,und",

    # Polling interval for --watch mode (seconds)
    "poll_interval": 10,

    # TMDb API
    "tmdb_api_key": "",

    # Jellyfin integration
    "jellyfin_url": "http://localhost:8096",
    "jellyfin_api_key": "",
}

# Global TUI instance (None when TUI is disabled)
tui = None  # type: RipperTUI | None

# Logger — initialized in cli.main()
log = None

# Stdout handler ref so TUI can suppress/restore it
_stdout_handler = None

# Pipeline summary lines to print after TUI stops
_pipeline_summary = []

# Graceful shutdown
_shutdown_requested = False
_active_processes = []
_active_processes_lock = threading.Lock()


def request_shutdown():
    """Set the shutdown flag."""
    global _shutdown_requested
    _shutdown_requested = True


def is_shutdown_requested():
    """Check whether shutdown has been requested."""
    return _shutdown_requested
