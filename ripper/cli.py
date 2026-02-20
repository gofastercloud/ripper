"""CLI entry point and argument parsing."""

import argparse
import json
import signal
import sys
from pathlib import Path

from ripper import state
from ripper.cleanup import cleanup_rips, repair_library, verify_rip_manifest
from ripper.config import (
    _CONFIGURABLE_KEYS,
    CONFIG_PATH,
    load_config,
    run_setup,
)
from ripper.helpers import (
    check_external_drive,
    ensure_dirs,
    hash_file,
    run_cmd,
    sanitize_filename,
    validate_api_keys,
)
from ripper.jellyfin import jellyfin_check_status, jellyfin_scan_library
from ripper.metadata import (
    download_artwork,
    get_disc_metadata,
    tmdb_get_season_episodes,
    write_episode_nfo,
    write_nfo,
    write_tv_nfo,
)
from ripper.pipeline import (
    compress_mkv,
    organize_file,
    organize_tv_episode,
    rip_disc,
    run_pipeline,
    watch_mode,
)
from ripper.tui import setup_logging

CONFIG = state.CONFIG


# ============================================================================
# SIGNAL HANDLING
# ============================================================================

def _signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    if state.is_shutdown_requested():
        print("\n\nForce quit. Intermediate files may remain in _rips/.")
        sys.exit(1)
    state.request_shutdown()
    print("\n\nShutting down gracefully (Ctrl+C again to force quit)...")
    print("Waiting for current operation(s) to finish...")
    with state._active_processes_lock:
        for proc in state._active_processes:
            try:
                proc.terminate()
            except OSError:
                pass


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ============================================================================
# DRIVE DETECTION
# ============================================================================

def check_drive():
    """Detect the Blu-ray drive and check LibreDrive / UHD compatibility."""
    import re

    makemkv = CONFIG["makemkv_bin"]

    if not Path(makemkv).exists():
        state.log.error(f"MakeMKV not found at {makemkv}")
        state.log.error("Install with: brew install --cask makemkv")
        return None

    state.log.info("Scanning for optical drives...")
    result = run_cmd([makemkv, "-r", "info", "disc:9999"], timeout=120)

    if result is None:
        state.log.error("Failed to query MakeMKV for drive info.")
        return None

    drive_info = {
        "found": False,
        "index": None,
        "name": None,
        "firmware": None,
        "libredrive": None,
        "uhd_capable": False,
    }

    for line in result.stdout.splitlines():
        if line.startswith("DRV:") and "/dev/" in line:
            parts = line.split(",")
            if len(parts) >= 7:
                drive_info["found"] = True
                drive_info["index"] = parts[0].split(":")[1]
                drive_info["name"] = parts[4].strip('"')
                drive_info["disc_label"] = parts[5].strip('"')
                drive_info["dev_path"] = parts[6].strip('"')

        if "opened in OS access mode" in line:
            match = re.search(r'"Optical drive \\"(.+?)\\"', line)
            if match:
                drive_info["firmware"] = match.group(1)

    if not drive_info["found"]:
        state.log.warning("No optical drive detected. Is a disc inserted?")
        return drive_info

    state.log.info("=" * 60)
    state.log.info("DRIVE COMPATIBILITY REPORT")
    state.log.info("=" * 60)
    state.log.info(f"  Drive:      {drive_info['name']}")
    state.log.info(f"  Device:     {drive_info.get('dev_path', 'Unknown')}")
    if drive_info.get("disc_label"):
        state.log.info(f"  Disc:       {drive_info['disc_label']}")
    if drive_info.get("firmware"):
        state.log.info(f"  Firmware:   {drive_info['firmware']}")

    if "BDR-UD04" in drive_info["name"] or "BDR-US04" in drive_info["name"]:
        drive_info["libredrive"] = True
        drive_info["uhd_capable"] = True
        state.log.info("  LibreDrive: ✅ ENABLED (Pioneer BDR-UD04 — known good)")
    else:
        state.log.info("  LibreDrive: will be checked during rip")

    state.log.info("=" * 60)
    return drive_info


# ============================================================================
# MAIN
# ============================================================================

def main():
    # Pre-parse just --init, --config, --show-config before full setup
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--init", action="store_true")
    pre.add_argument("--config", metavar="PATH")
    pre.add_argument("--show-config", action="store_true")
    pre_args, _ = pre.parse_known_args()

    config_file = Path(pre_args.config) if pre_args.config else CONFIG_PATH
    load_config(pre_args.config)

    if pre_args.init:
        run_setup()
        return

    if not config_file.exists() and sys.stdin.isatty():
        run_setup(first_run=True)
        load_config(pre_args.config)

    if pre_args.show_config:
        print(json.dumps(
            {k: CONFIG[k] for k in sorted(_CONFIGURABLE_KEYS)
             if k in CONFIG and CONFIG[k] is not None},
            indent=2,
        ))
        print(f"\nConfig file: {config_file}")
        return

    state.log = setup_logging()

    parser = argparse.ArgumentParser(
        description="4K Blu-ray Rip → Compress → Organize Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  uv run ripper                          Auto-detect and rip inserted disc
  uv run ripper --title "The Matrix"     Rip with a specific title
  uv run ripper --tv --season 2          Rip a TV disc (season 2)
  uv run ripper --hq                     Use software x265 (slower, best quality)
  uv run ripper --status                 Check drive, Jellyfin, API keys
  uv run ripper --reencode _rips/dir     Re-encode from preserved rips
  uv run ripper --cleanup                Free space by deleting old rips
  uv run ripper --repair                 Fix missing artwork/NFO in library
  uv run ripper --init                   Reconfigure settings

config:
  ~/.config/ripper/config.json (created on first run)
  Edit directly, or run --init to reconfigure.
  Env vars TMDB_API_KEY and JELLYFIN_API_KEY override the config file.
""",
    )

    parser.add_argument("--init", action="store_true", help="Run the first-time setup wizard")
    parser.add_argument("--config", metavar="PATH", help="Path to config file")
    parser.add_argument("--show-config", action="store_true", help="Print current configuration")

    parser.add_argument("--title", "-t", help="Movie/show title")
    parser.add_argument("--year", "-y", help="Release year")

    parser.add_argument("--tv", action="store_true", help="Force TV show mode")
    parser.add_argument("--season", "-s", type=int, help="Season number (for TV shows)")
    parser.add_argument("--episode", "-e", type=int, default=1, help="Starting episode number (default: 1)")

    parser.add_argument("--check-drive", action="store_true", help="Only check drive compatibility")
    parser.add_argument("--watch", "-w", action="store_true", help="Watch for disc insertion and auto-rip")
    parser.add_argument("--rip-only", action="store_true", help="Only rip, don't compress")
    parser.add_argument("--compress-only", metavar="MKV_PATH", help="Only compress an existing MKV file")

    parser.add_argument("--hq", action="store_true", help="Use software x265 encoder (slower, best quality)")
    parser.add_argument("--grain", action="store_true", help="Use x265 'grain' tune (implies --hq)")
    parser.add_argument("--quality", "-q", type=int, help=f"Override RF quality value (default: {CONFIG['quality_rf']})")

    parser.add_argument("--status", action="store_true", help="Check status of drive, Jellyfin, and external storage")
    parser.add_argument("--scan", action="store_true", help="Trigger a Jellyfin library scan")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the metadata cache")
    parser.add_argument("--cleanup", action="store_true", help="Delete preserved raw rips to free space")
    parser.add_argument("--reencode", metavar="RIP_DIR", help="Re-encode from existing rips")
    parser.add_argument("--verify-rips", metavar="RIP_DIR", help="Verify rip integrity using MD5 hashes")
    parser.add_argument("--repair", action="store_true", help="Scan library for missing artwork/NFO and re-fetch")

    args = parser.parse_args()

    # Apply overrides
    if args.hq or args.grain:
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

    state.log.info("4K Blu-ray Pipeline")
    state.log.info(f"Output drive: {CONFIG['output_base']}")
    state.log.info("")

    if args.clear_cache:
        cache_path = Path(CONFIG["metadata_cache"])
        if cache_path.exists():
            cache_path.unlink()
            state.log.info("Metadata cache cleared.")
        else:
            state.log.info("No cache file found.")
        return

    if args.cleanup:
        cleanup_rips()
        return

    if args.repair:
        repair_library()
        return

    if args.verify_rips:
        rip_dir = Path(args.verify_rips)
        if not rip_dir.exists():
            state.log.error(f"Directory not found: {rip_dir}")
            return
        state.log.info(f"Verifying rips in {rip_dir}...")
        manifest_path = rip_dir / "rip_manifest.json"
        if not manifest_path.exists():
            state.log.error("No manifest found. Cannot verify.")
            return
        with open(manifest_path) as f:
            manifest = json.load(f)
        all_ok = True
        for entry in manifest.get("files", []):
            fpath = rip_dir / entry["filename"]
            if not fpath.exists():
                state.log.error(f"  MISSING: {entry['filename']}")
                all_ok = False
                continue
            if fpath.stat().st_size != entry["size_bytes"]:
                state.log.error(f"  SIZE MISMATCH: {entry['filename']}")
                all_ok = False
                continue
            state.log.info(f"  Verifying MD5: {entry['filename']}...")
            actual_hash = hash_file(fpath)
            if actual_hash != entry["md5"]:
                state.log.error(f"  HASH MISMATCH: {entry['filename']} (expected {entry['md5']}, got {actual_hash})")
                all_ok = False
            else:
                state.log.info(f"  OK: {entry['filename']}")
        if all_ok:
            state.log.info("All files verified OK.")
        else:
            state.log.error("Some files failed verification!")
        return

    if args.reencode:
        rip_dir = Path(args.reencode)
        if not rip_dir.exists():
            state.log.error(f"Directory not found: {rip_dir}")
            return
        valid = verify_rip_manifest(rip_dir)
        if valid is None:
            mkv_files = sorted(rip_dir.glob("*.mkv"))
            if not mkv_files:
                state.log.error(f"No MKV files found in {rip_dir}")
                return
            state.log.info(f"No manifest found. Found {len(mkv_files)} MKV file(s).")
            valid = [(f, None) for f in mkv_files]
        elif not valid:
            state.log.error("No valid rips found.")
            return

        state.log.info(f"Re-encoding {len(valid)} file(s) from {rip_dir}...")

        first_mkv = valid[0][0]
        state.log.info(f"  Probing {first_mkv.name} for source format...")

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
                state.log.info(f"Re-encode complete: {final}")
        else:
            season_num = disc_info.get("season") or 1
            ep_start = disc_info.get("start_episode") or 1
            episodes_info = []
            if meta and meta.get("tmdb_id"):
                episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], season_num)

            for idx, (mkv_path, ep_num) in enumerate(valid):
                if state.is_shutdown_requested():
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
                state.log.info(f"\n  Re-encoding {ep_tag}: {ep_name or 'Episode ' + str(ep_num)}")
                encoded = compress_mkv(mkv_path, encode_name, output_dir=CONFIG["rip_dir"])
                if not encoded:
                    state.log.error(f"  Failed to encode {ep_tag}")
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
            state.log.info("Re-encode complete.")
        return

    if args.status:
        state.log.info("=" * 60)
        state.log.info("SYSTEM STATUS")
        state.log.info("=" * 60)
        check_external_drive()
        validate_api_keys()
        check_drive()
        jf_ok = jellyfin_check_status()
        if not jf_ok:
            state.log.warning("  Jellyfin: ❌ Not reachable at " + CONFIG["jellyfin_url"])
        if CONFIG["jellyfin_api_key"]:
            state.log.info("  Jellyfin API: ✅ Key configured")
        else:
            state.log.warning("  Jellyfin API: ❌ No key set (export JELLYFIN_API_KEY)")
        movies_dir = Path(CONFIG["encode_dir"])
        tv_dir = Path(CONFIG["tv_encode_dir"])
        if movies_dir.exists():
            movie_count = sum(1 for d in movies_dir.iterdir() if d.is_dir())
            state.log.info(f"  Movies: {movie_count} in {movies_dir}")
        if tv_dir.exists():
            show_count = sum(1 for d in tv_dir.iterdir() if d.is_dir())
            state.log.info(f"  TV Shows: {show_count} in {tv_dir}")
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
            state.log.error(f"File not found: {mkv_path}")
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

    if not validate_api_keys():
        state.log.error("Fix the above errors and try again.")
        return

    media_type = "tv" if args.tv else None
    run_pipeline(
        title_name=args.title,
        year=args.year,
        media_type=media_type,
        season=args.season,
        start_episode=args.episode,
    )
