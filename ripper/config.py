"""Configuration loading, saving, and setup wizard."""

import json
import os
import subprocess
from pathlib import Path

from ripper import state

CONFIG = state.CONFIG

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
    "makemkv_bin", "handbrake_bin", "ffprobe_bin",
    "min_title_length", "min_episode_length",
    "encoder", "quality_rf", "encoder_preset", "encoder_tune",
    "encoder_profile", "encoder_level", "hq_mode", "output_format",
    "viewing_profile", "quality_rf_dvd", "quality_rf_bd", "quality_rf_uhd",
    "audio_mode", "audio_lang",
    "poll_interval",
    "tmdb_api_key", "metadata_cache",
    "jellyfin_url", "jellyfin_api_key",
}


def _find_binary(name, mac_app_path=None):
    """Try to find a binary: check PATH first, then common macOS locations."""
    try:
        result = subprocess.run(
            ["which", name], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
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
    if not CONFIG.get("makemkv_bin"):
        found = _find_binary(
            "makemkvcon",
            "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
        )
        if found:
            CONFIG["makemkv_bin"] = found

    if not CONFIG.get("handbrake_bin"):
        found = _find_binary("HandBrakeCLI")
        if found:
            CONFIG["handbrake_bin"] = found
        else:
            CONFIG["handbrake_bin"] = "HandBrakeCLI"

    if not CONFIG.get("ffprobe_bin"):
        found = _find_binary("ffprobe")
        if found:
            CONFIG["ffprobe_bin"] = found

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

    # 4. Viewing profile
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
    CONFIG["quality_rf"] = profile["quality_rf_bd"]
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

    path = save_config()
    print(f"\n  Config saved to {path}")
    print("  Run --status to verify everything works.")
    print()
