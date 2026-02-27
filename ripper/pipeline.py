"""Main pipeline orchestration: rip, compress, organize, scan."""

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ripper import state
from ripper.cleanup import write_history_record, write_rip_manifest
from ripper.helpers import (
    check_external_drive,
    eject_disc,
    ensure_dirs,
    run_cmd,
    run_cmd_progress,
    sanitize_filename,
)
from ripper.jellyfin import jellyfin_scan_library
from ripper.media import (
    auto_tune_for_source,
    detect_source_format,
    handbrake_progress,
    log_encode_stats,
    makemkv_progress,
    probe_media_file,
)
from ripper.metadata import (
    download_artwork,
    get_disc_metadata,
    tmdb_get_season_episodes,
    write_episode_nfo,
    write_nfo,
    write_tv_nfo,
)
from ripper.tui import RipperTUI, poster_to_ascii

CONFIG = state.CONFIG

# Keys set by auto_tune_for_source() and read by compress_mkv()
_ENCODER_CONFIG_KEYS = [
    "encoder", "quality_rf", "encoder_preset", "encoder_profile",
    "encoder_level", "hq_mode", "deinterlace", "encoder_tune",
    "audio_mode", "audio_lang",
]


@dataclass
class EncodeJob:
    """A single encode job for the watch-mode encoder worker."""
    job_type: str           # "movie" or "tv_episode"
    title_name: str
    year: str | None
    rip_mkv: Path
    encoder_config: dict
    metadata: dict | None
    source_info: dict
    season_num: int | None = None
    episode_num: int | None = None
    episode_name: str = ""
    episode_info: dict | None = None
    display_label: str = ""
    queue_id: str = ""


def snapshot_encoder_config():
    """Capture encoder-relevant CONFIG keys into a dict."""
    return {k: CONFIG[k] for k in _ENCODER_CONFIG_KEYS}


def restore_encoder_config(snap):
    """Restore encoder-relevant CONFIG keys from a snapshot."""
    for k, v in snap.items():
        CONFIG[k] = v


# ============================================================================
# STEP 1: RIP WITH MAKEMKV
# ============================================================================

def rip_disc(title_name=None):
    """Rip the main feature from the inserted disc. Returns (mkv_path, source_info) or (None, None)."""
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_title_length"]

    if title_name:
        rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(title_name)
    else:
        rip_out = Path(CONFIG["rip_dir"]) / f"rip_{datetime.now():%Y%m%d_%H%M%S}"

    rip_out.mkdir(parents=True, exist_ok=True)

    # Clean stale MKV files from previous failed rips
    for old_mkv in rip_out.glob("*.mkv"):
        old_size = old_mkv.stat().st_size
        if old_size < 100_000_000:  # < 100 MB is a failed rip remnant
            state.log.info(f"  Removing stale rip: {old_mkv.name} ({old_size / 1e6:.1f} MB)")
            old_mkv.unlink()

    state.log.info("=" * 60)
    state.log.info("STEP 1: RIPPING DISC WITH MAKEMKV")
    state.log.info("=" * 60)

    state.log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "--minlength=0", "info", "disc:0"], timeout=300)
    if scan is None or scan.returncode != 0:
        state.log.error("Failed to scan disc. Is a disc inserted?")
        return None, None

    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

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
        state.log.error("No titles found on disc.")
        return None, None

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

        name = attrs.get(2, f"Title {tid}")
        size_bytes = int(attrs.get(11, 0))
        size_gb = size_bytes / (1024 ** 3) if size_bytes else 0
        state.log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

    if best_title is None or best_duration < min_length:
        state.log.error(f"No title found longer than {min_length // 60} minutes.")
        return None, None

    state.log.info(f"Selected title {best_title} ({best_duration // 60} min)")

    state.log.info(f"Ripping to: {rip_out}")
    start_time = time.time()
    returncode, stderr = run_cmd_progress(
        [
            makemkv, "-r", "mkv", "disc:0",
            str(best_title), str(rip_out),
            "--minlength=0", "--progress=-stdout",
        ],
        timeout=7200,
        progress_handler=makemkv_progress,
    )
    if not (state.tui and state.tui.enabled):
        print()

    rip_elapsed = time.time() - start_time
    rip_min = int(rip_elapsed // 60)
    state.log.info(f"Rip completed in {rip_min} minutes.")

    if returncode is None or returncode != 0:
        state.log.error("MakeMKV rip failed!")
        if stderr:
            state.log.error(stderr[-500:])
        return None, None

    mkv_files = list(rip_out.glob("*.mkv"))
    if not mkv_files:
        state.log.error("No MKV file found after rip!")
        return None, None

    mkv_file = max(mkv_files, key=lambda f: f.stat().st_size)
    size_bytes = mkv_file.stat().st_size
    size_gb = size_bytes / (1024 ** 3)
    state.log.info(f"Rip complete: {mkv_file.name} ({size_gb:.1f} GB)")

    if size_bytes < 100_000_000:  # < 100 MB is effectively a failed rip
        state.log.error(f"Ripped file is too small ({size_bytes / 1e6:.1f} MB) — MakeMKV likely failed.")
        state.log.error("Try ejecting and re-inserting the disc, or check MakeMKV logs.")
        if stderr:
            state.log.error(f"MakeMKV stderr: {stderr[-1000:]}")
        return None, None

    return mkv_file, source_info


# ============================================================================
# STEP 2: COMPRESS WITH HANDBRAKE
# ============================================================================

def compress_mkv(input_mkv, title_name=None, output_dir=None):
    """Compress with HandBrake, preserving HDR10 and Dolby Vision metadata."""
    hb = CONFIG["handbrake_bin"]

    if title_name:
        out_name = sanitize_filename(title_name)
    else:
        out_name = input_mkv.stem

    dest = Path(output_dir) if output_dir else Path(CONFIG["encode_dir"])
    dest.mkdir(parents=True, exist_ok=True)
    output_file = dest / f"{out_name}.mkv"

    if output_file.exists():
        counter = 1
        while output_file.exists():
            output_file = dest / f"{out_name} ({counter}).mkv"
            counter += 1

    is_hq = CONFIG.get("hq_mode", False)
    mode_label = "SOFTWARE x265 (HQ)" if is_hq else "VIDEOTOOLBOX (HW)"

    state.log.info("=" * 60)
    state.log.info("STEP 2: COMPRESSING WITH HANDBRAKE")
    state.log.info("=" * 60)
    state.log.info(f"  Input:   {input_mkv}")
    state.log.info(f"  Output:  {output_file}")
    state.log.info(f"  Mode:    {mode_label}")
    state.log.info(f"  Encoder: {CONFIG['encoder']} @ RF {CONFIG['quality_rf']}")
    state.log.info(f"  Preset:  {CONFIG['encoder_preset']}")
    state.log.info("")
    if is_hq:
        state.log.info("  HQ mode: software x265 — slower but best quality.")
        state.log.info("  This will take a while for 4K. Go grab a coffee (or three).")
    else:
        state.log.info("  HW mode: VideoToolbox — fast encoding via Apple Silicon.")
    state.log.info("")

    cmd = [
        hb,
        "--input", str(input_mkv),
        "--output", str(output_file),
        "--format", "av_mkv",
        "--encoder", CONFIG["encoder"],
        "--quality", str(CONFIG["quality_rf"]),
        "--encoder-preset", CONFIG["encoder_preset"],
        "--encoder-profile", CONFIG["encoder_profile"],
        "--encoder-level", CONFIG["encoder_level"],
        "--pfr",
    ]

    if is_hq:
        cmd.extend([
            "--encopts",
            "aq-mode=3:rd=4:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=60:bframes=8:ref=5",
        ])

    cmd.extend([
        "--non-anamorphic",
        "--crop", "0:0:0:0",
        "--audio-lang-list", CONFIG.get("audio_lang", "eng,und"),
        "--all-audio",
        "--audio-fallback", "aac",
        "--all-subtitles",
    ])

    audio_mode = CONFIG.get("audio_mode", "copy,aac")
    if audio_mode == "aac":
        cmd.extend(["--aencoder", "aac", "--mixdown", "stereo"])
    else:
        cmd.extend(["--aencoder", "copy,aac", "--mixdown", "none,stereo"])

    if CONFIG.get("deinterlace"):
        cmd.extend(["--comb-detect", "--decomb"])
        state.log.info("  Deinterlace: enabled (comb-detect + decomb)")

    if CONFIG["encoder_tune"] and is_hq:
        cmd.extend(["--encoder-tune", CONFIG["encoder_tune"]])

    start_time = time.time()
    returncode, stderr = run_cmd_progress(
        cmd, timeout=86400, progress_handler=handbrake_progress
    )
    if not (state.tui and state.tui.enabled):
        print()

    if returncode is None or returncode != 0:
        state.log.error("HandBrake encoding failed!")
        if stderr:
            state.log.error(stderr[-1000:])
        return None

    if not output_file.exists():
        state.log.error(f"HandBrake exited OK but output file missing: {output_file}")
        if stderr:
            state.log.error(stderr[-1000:])
        return None

    elapsed = time.time() - start_time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)

    in_size = input_mkv.stat().st_size / (1024 ** 3)
    out_size = output_file.stat().st_size / (1024 ** 3)
    ratio = (1 - out_size / in_size) * 100

    state.log.info(f"Encode complete in {hours}h {minutes}m {seconds}s")
    state.log.info(f"  Source:  {in_size:.1f} GB")
    state.log.info(f"  Output:  {out_size:.1f} GB")
    state.log.info(f"  Savings: {ratio:.0f}%")

    probe = probe_media_file(output_file)
    if probe:
        raw_bytes = input_mkv.stat().st_size if input_mkv.exists() else None
        log_encode_stats(probe, raw_size_bytes=raw_bytes)

    return output_file


# ============================================================================
# STEP 3: ORGANIZE
# ============================================================================

def organize_file(encoded_file, title_name, year=None):
    """Move encoded file to Plex/Jellyfin naming: Movies/Title (Year)/Title (Year).mkv"""
    state.log.info("=" * 60)
    state.log.info("STEP 3: ORGANIZING")
    state.log.info("=" * 60)

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

    state.log.info(f"  Final location: {dest_file}")
    return dest_file


def organize_tv_episode(encoded_file, show_title, year, season_num, episode_num, episode_name=""):
    """Move encoded TV episode to Jellyfin naming convention."""
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

    state.log.info(f"  {ep_tag}: {dest_file.name}")
    return dest_file


# ============================================================================
# TV SHOW: MULTI-EPISODE RIP
# ============================================================================

def rip_tv_disc(title_name, season_num, start_episode=1, episodes_info=None):
    """Rip all episode-length titles from a TV disc."""
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_episode_length"]

    rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(f"{title_name}_S{season_num:02d}")
    rip_out.mkdir(parents=True, exist_ok=True)

    state.log.info("=" * 60)
    state.log.info("STEP 1: RIPPING TV DISC WITH MAKEMKV")
    state.log.info(f"  Show: {title_name} — Season {season_num}")
    state.log.info("=" * 60)

    state.log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "--minlength=0", "info", "disc:0"], timeout=300)
    if scan is None or scan.returncode != 0:
        state.log.error("Failed to scan disc.")
        return []

    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

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
        state.log.error("No titles found on disc.")
        return []

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

        state.log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

        if seconds >= min_length:
            episode_candidates.append((tid, seconds, size_bytes, name))

    if not episode_candidates:
        state.log.error(f"No titles found longer than {min_length // 60} minutes.")
        return []

    episode_candidates.sort(key=lambda x: x[0])

    if len(episode_candidates) > 2:
        durations = [c[1] for c in episode_candidates]
        avg_duration = sum(durations) / len(durations)
        filtered = [c for c in episode_candidates if c[1] < avg_duration * 1.8]
        if len(filtered) >= 2:
            removed = len(episode_candidates) - len(filtered)
            if removed > 0:
                state.log.info(f"  Filtered out {removed} 'play all' title(s)")
            episode_candidates = filtered

    state.log.info(f"\n  Found {len(episode_candidates)} episodes to rip")

    _ep_names = {}
    if episodes_info:
        for ei in episodes_info:
            _ep_names[ei.get("episode_number")] = ei.get("name", "")
        state.log.info("  Episode names from TMDb:")
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_name = _ep_names.get(ep_num, "Unknown")
            state.log.info(f"    E{ep_num:02d}: {ep_name}")

    ripped = []
    for idx, (tid, seconds, _size_bytes, _name) in enumerate(episode_candidates):
        if state.is_shutdown_requested():
            state.log.info("  Stopping — Ctrl+C received. Partial rips preserved in _rips/.")
            break
        ep_num = start_episode + idx
        tmdb_name = _ep_names.get(ep_num, "")
        display = f"E{ep_num:02d}"
        if tmdb_name:
            display += f" — {tmdb_name}"
        state.log.info(f"\n  Ripping {display} (title {tid}, {seconds // 60} min)...")

        returncode, _stderr = run_cmd_progress(
            [
                makemkv, "-r", "mkv", "disc:0",
                str(tid), str(rip_out),
                "--minlength=0", "--progress=-stdout",
            ],
            timeout=3600,
            progress_handler=makemkv_progress,
        )
        if not (state.tui and state.tui.enabled):
            print()

        if returncode is None or returncode != 0:
            state.log.error(f"  Failed to rip title {tid}")
            continue

        mkv_files = sorted(rip_out.glob("*.mkv"), key=lambda f: f.stat().st_mtime)
        if mkv_files:
            latest = mkv_files[-1]
            size_gb = latest.stat().st_size / (1024 ** 3)
            state.log.info(f"  Ripped: {latest.name} ({size_gb:.1f} GB)")
            ripped.append((latest, ep_num))

    state.log.info(f"\n  Ripped {len(ripped)} episodes total")
    return ripped


def _rip_tv_disc_parallel(title_name, season_num, start_episode, episodes_info,
                           encode_queue, rip_dirs_to_clean):
    """Rip TV episodes and push each to encode_queue immediately after ripping."""
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_episode_length"]

    rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(f"{title_name}_S{season_num:02d}")
    rip_out.mkdir(parents=True, exist_ok=True)
    rip_dirs_to_clean.add(rip_out)

    state.log.info("=" * 60)
    state.log.info("STEP 1: RIPPING TV DISC (parallel encode enabled)")
    state.log.info(f"  Show: {title_name} — Season {season_num}")
    state.log.info("=" * 60)

    state.log.info("Scanning disc for titles...")
    scan = run_cmd([makemkv, "-r", "--minlength=0", "info", "disc:0"], timeout=300)
    if scan is None or scan.returncode != 0:
        state.log.error("Failed to scan disc.")
        return []

    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)

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
        state.log.error("No titles found on disc.")
        return []

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

        state.log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

        if seconds >= min_length:
            episode_candidates.append((tid, seconds, size_bytes, name))

    if not episode_candidates:
        state.log.error(f"No titles found longer than {min_length // 60} minutes.")
        return []

    episode_candidates.sort(key=lambda x: x[0])

    if len(episode_candidates) > 2:
        durations = [c[1] for c in episode_candidates]
        avg_duration = sum(durations) / len(durations)
        filtered = [c for c in episode_candidates if c[1] < avg_duration * 1.8]
        if len(filtered) >= 2:
            removed = len(episode_candidates) - len(filtered)
            if removed > 0:
                state.log.info(f"  Filtered out {removed} 'play all' title(s)")
            episode_candidates = filtered

    state.log.info(f"\n  Found {len(episode_candidates)} episodes to rip")

    _ep_names = {}
    if episodes_info:
        for ei in episodes_info:
            _ep_names[ei.get("episode_number")] = ei.get("name", "")
        state.log.info("  Episode names from TMDb:")
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_name = _ep_names.get(ep_num, "Unknown")
            state.log.info(f"    E{ep_num:02d}: {ep_name}")

    state.log.info("\n  Parallel mode: encoding starts as soon as each episode is ripped\n")

    if state.tui and state.tui.enabled:
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_tag = f"S{season_num:02d}E{ep_num:02d}"
            ep_name = _ep_names.get(ep_num, "")
            label = f"{ep_tag} — {ep_name}" if ep_name else ep_tag
            state.tui.add_queue_item(ep_tag, label)

    ripped = []
    for idx, (tid, seconds, _size_bytes, _name) in enumerate(episode_candidates):
        if state.is_shutdown_requested():
            state.log.info("  [RIP] Stopping — Ctrl+C received.")
            break
        ep_num = start_episode + idx
        tmdb_name = _ep_names.get(ep_num, "")
        ep_tag = f"S{season_num:02d}E{ep_num:02d}"
        display = f"E{ep_num:02d}"
        if tmdb_name:
            display += f" — {tmdb_name}"
        state.log.info(f"  [RIP] {display} (title {tid}, {seconds // 60} min)...")

        if state.tui and state.tui.enabled:
            state.tui.update_queue_item(ep_tag, "ripping")
            state.tui.update_rip(0.0, task=display)

        returncode, _stderr = run_cmd_progress(
            [
                makemkv, "-r", "mkv", "disc:0",
                str(tid), str(rip_out),
                "--minlength=0", "--progress=-stdout",
            ],
            timeout=3600,
            progress_handler=makemkv_progress,
        )
        if not (state.tui and state.tui.enabled):
            print()

        if returncode is None or returncode != 0:
            state.log.error(f"  [RIP] Failed title {tid}")
            if state.tui and state.tui.enabled:
                state.tui.update_queue_item(ep_tag, "failed")
            continue

        if state.tui and state.tui.enabled:
            state.tui.update_queue_item(ep_tag, "ripped")
            state.tui.update_rip(100.0)

        mkv_files = sorted(rip_out.glob("*.mkv"), key=lambda f: f.stat().st_mtime)
        if mkv_files:
            latest = mkv_files[-1]
            size_gb = latest.stat().st_size / (1024 ** 3)
            state.log.info(f"  [RIP] Done: {latest.name} ({size_gb:.1f} GB) → queued for encode")
            ripped.append((latest, ep_num))
            encode_queue.put((latest, ep_num))

    state.log.info(f"\n  [RIP] Finished — {len(ripped)} episodes ripped")
    if state.tui and state.tui.enabled:
        state.tui.update_rip(100.0, task="All done")
    return ripped


# ============================================================================
# FULL PIPELINE
# ============================================================================

def run_pipeline(title_name=None, year=None, media_type=None,
                 season=None, start_episode=None, disc=None):
    """Run the complete rip → compress → organize pipeline."""
    ensure_dirs()

    if not check_external_drive():
        return False

    from ripper.cli import check_drive
    drive = check_drive()
    if drive and not drive["found"]:
        return False

    disc_info = get_disc_metadata(
        manual_title=title_name,
        manual_year=year,
        media_type=media_type,
        season=season,
        start_episode=start_episode,
        disc=disc,
    )

    title_name = disc_info["title"]
    year = disc_info.get("year")
    media_type = disc_info.get("media_type", "movie")
    meta = disc_info.get("metadata")

    if state.is_shutdown_requested():
        state.log.info("Pipeline cancelled.")
        return False

    if not title_name:
        state.log.error("No title could be determined. Use --title to specify manually.")
        return False

    state.log.info(f"\nStarting pipeline for [{media_type.upper()}]: {title_name}"
                   + (f" ({year})" if year else ""))
    state.log.info(f"Start time: {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    if sys.stdout.isatty():
        state.tui = RipperTUI()
        state.tui.set_metadata(title_name, year, media_type)

        poster_path = meta.get("poster_path") if meta else None
        if poster_path:
            art = poster_to_ascii(poster_path)
            if art:
                state.tui.set_poster(art)

        state.tui.start()
        state.tui.start_disc_timer()

    try:
        result = _run_pipeline_inner(
            title_name, year, media_type, meta, disc_info,
        )
    finally:
        if state.tui:
            state.tui.stop()
            state.tui = None

    # Re-print summary to stdout now that TUI is gone
    if result and state._pipeline_summary:
        print()
        for line in state._pipeline_summary:
            print(line)
        print()
        state._pipeline_summary = []

    return result


def _run_pipeline_inner(title_name, year, media_type, meta, disc_info):
    """Inner pipeline logic."""

    # ---- MOVIE PIPELINE ----
    if media_type == "movie":
        if state.tui and state.tui.enabled:
            state.tui.add_queue_item("movie", title_name)
            state.tui.update_queue_item("movie", "ripping")
            state.tui.update_rip(0.0, task="Main feature")
        rip_start = time.time()
        rip_mkv, _source_info = rip_disc(title_name)
        if state.is_shutdown_requested():
            state.log.info("Pipeline cancelled during rip. Raw files preserved in _rips/.")
            return False
        if not rip_mkv:
            state.log.error("Pipeline failed at rip stage.")
            return False
        if state.tui and state.tui.enabled:
            state.tui.update_queue_item("movie", "ripped")
            state.tui.update_rip(100.0)
            state.tui.log(f"Rip complete: {rip_mkv.name}")

        eject_disc()
        write_rip_manifest(rip_mkv.parent, [(rip_mkv, None)])

        rip_elapsed = time.time() - rip_start

        if state.tui and state.tui.enabled:
            state.tui.update_queue_item("movie", "encoding")
            state.tui.update_encode(0.0, task=title_name)
        encode_start = time.time()
        encoded = compress_mkv(rip_mkv, title_name)
        if not encoded:
            state.log.error("Pipeline failed at encode stage. Raw rip preserved in _rips/.")
            return False
        if state.tui and state.tui.enabled:
            state.tui.update_queue_item("movie", "done")
            state.tui.update_encode(100.0)
            state.tui.log(f"Encode complete: {encoded.name}")

        final = organize_file(encoded, title_name, year)

        final_dir = final.parent
        if meta:
            folder_name = sanitize_filename(
                f"{title_name} ({year})" if year else title_name
            )
            write_nfo(meta, final_dir, folder_name)
            download_artwork(meta, final_dir)

        rip_size = rip_mkv.stat().st_size / (1024 ** 3) if rip_mkv.exists() else 0
        state.log.info(f"Raw rip preserved in _rips/ ({rip_size:.1f} GB). Use --cleanup to remove.")

        # Write history record
        encode_elapsed = time.time() - encode_start
        raw_gb = rip_mkv.stat().st_size / (1024 ** 3) if rip_mkv.exists() else None
        enc_gb = final.stat().st_size / (1024 ** 3) if final.exists() else None
        probe = probe_media_file(final)
        _source = _source_info or {}
        history_rec = {
            "title": title_name,
            "year": year,
            "media_type": "movie",
            "disc_type": _source.get("disc_type"),
            "resolution": f"{_source.get('width', '')}x{_source.get('height', '')}" if _source.get("width") else None,
            "date": datetime.now().isoformat(timespec="seconds"),
            "rip_time_min": int(rip_elapsed // 60),
            "encode_time_min": int(encode_elapsed // 60),
            "raw_size_gb": round(raw_gb, 1) if raw_gb else None,
            "encoded_size_gb": round(enc_gb, 1) if enc_gb else None,
            "compression_pct": round((1 - enc_gb / raw_gb) * 100) if raw_gb and enc_gb and raw_gb > 0 else None,
            "video_codec": probe.get("video_codec", "").upper() + (" " + probe.get("video_profile", "")).rstrip() if probe else None,
            "hdr": probe.get("hdr") if probe else None,
            "audio": " + ".join(
                t["codec"].upper() + (" " + t["layout"] if t["layout"] else f" {t['channels']}ch")
                for t in probe.get("audio_tracks", [])
            ) if probe else None,
            "encoder": CONFIG.get("encoder"),
            "quality_rf": CONFIG.get("quality_rf"),
            "episodes": None,
        }
        try:
            write_history_record(history_rec)
        except Exception as e:
            state.log.warning(f"Failed to write history record: {e}")

    # ---- TV SHOW PIPELINE (parallel rip + encode) ----
    else:
        season_num = disc_info.get("season") or 1
        ep_start = disc_info.get("start_episode") or 1

        episodes_info = []
        if meta and meta.get("tmdb_id"):
            episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], season_num)

        ep_name_map = {}
        ep_info_map = {}
        for ei in episodes_info:
            ep_name_map[ei.get("episode_number")] = ei.get("name", "")
            ep_info_map[ei.get("episode_number")] = ei

        encode_q = queue.Queue()
        encode_results = []
        rip_dirs_to_clean = set()

        def _encode_worker():
            while True:
                item = encode_q.get()
                if item is None:
                    break
                if state.is_shutdown_requested():
                    encode_q.task_done()
                    continue

                rip_mkv, ep_num = item
                ep_name = ep_name_map.get(ep_num, "")
                ep_info = ep_info_map.get(ep_num)
                ep_tag = f"S{season_num:02d}E{ep_num:02d}"
                encode_name = f"{sanitize_filename(title_name)} - {ep_tag}"

                state.log.info(f"\n  [ENCODE] {ep_tag}: {ep_name or 'Episode ' + str(ep_num)}")

                if state.tui and state.tui.enabled:
                    state.tui.update_queue_item(ep_tag, "encoding")
                    state.tui.update_encode(0.0, task=f"{ep_tag} — {ep_name}" if ep_name else ep_tag)

                ep_raw_size = rip_mkv.stat().st_size if rip_mkv.exists() else 0
                ep_encode_start = time.time()
                encoded = compress_mkv(rip_mkv, encode_name,
                                       output_dir=CONFIG["rip_dir"])
                if not encoded:
                    state.log.error(f"  [ENCODE] Failed {ep_tag}")
                    if state.tui and state.tui.enabled:
                        state.tui.update_queue_item(ep_tag, "failed")
                    encode_q.task_done()
                    continue
                ep_encode_elapsed = time.time() - ep_encode_start

                final = organize_tv_episode(
                    encoded, title_name, year, season_num, ep_num, ep_name
                )
                write_episode_nfo(ep_info, final, title_name, season_num, ep_num)
                encode_results.append((ep_num, final))

                if state.tui and state.tui.enabled:
                    state.tui.update_queue_item(ep_tag, "done")
                    state.tui.update_encode(100.0)
                    state.tui.log(f"{ep_tag} complete: {final.name}")

                # Write per-episode history record
                ep_enc_gb = final.stat().st_size / (1024 ** 3) if final.exists() else None
                ep_raw_gb = ep_raw_size / (1024 ** 3) if ep_raw_size else None
                ep_probe = probe_media_file(final)
                ep_history = {
                    "title": f"{title_name} {ep_tag}",
                    "year": year,
                    "media_type": "tv",
                    "disc_type": None,
                    "resolution": None,
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "rip_time_min": None,
                    "encode_time_min": int(ep_encode_elapsed // 60),
                    "raw_size_gb": round(ep_raw_gb, 1) if ep_raw_gb else None,
                    "encoded_size_gb": round(ep_enc_gb, 1) if ep_enc_gb else None,
                    "compression_pct": round((1 - ep_enc_gb / ep_raw_gb) * 100) if ep_raw_gb and ep_enc_gb and ep_raw_gb > 0 else None,
                    "video_codec": ep_probe.get("video_codec", "").upper() + (" " + ep_probe.get("video_profile", "")).rstrip() if ep_probe else None,
                    "hdr": ep_probe.get("hdr") if ep_probe else None,
                    "audio": " + ".join(
                        t["codec"].upper() + (" " + t["layout"] if t["layout"] else f" {t['channels']}ch")
                        for t in ep_probe.get("audio_tracks", [])
                    ) if ep_probe else None,
                    "encoder": CONFIG.get("encoder"),
                    "quality_rf": CONFIG.get("quality_rf"),
                    "episodes": 1,
                }
                try:
                    write_history_record(ep_history)
                except Exception as e:
                    state.log.warning(f"Failed to write history record: {e}")

                jellyfin_scan_library(quiet=True)
                encode_q.task_done()

        encoder_thread = threading.Thread(target=_encode_worker, daemon=True)
        encoder_thread.start()

        ripped_episodes = _rip_tv_disc_parallel(
            title_name, season_num, ep_start, episodes_info,
            encode_q, rip_dirs_to_clean,
        )

        eject_disc()

        if not ripped_episodes and not encode_results:
            state.log.error("Pipeline failed at rip stage — no episodes ripped.")
            encode_q.put(None)
            encoder_thread.join(timeout=5)
            return False

        encode_q.put(None)
        state.log.info("\n  Waiting for final encode to finish...")
        encoder_thread.join()

        if meta:
            show_dir = Path(CONFIG["tv_encode_dir"]) / sanitize_filename(
                f"{title_name} ({year})" if year else title_name
            )
            show_dir.mkdir(parents=True, exist_ok=True)
            write_tv_nfo(meta, show_dir, title_name, season_num)
            download_artwork(meta, show_dir)

        for rip_dir in rip_dirs_to_clean:
            if rip_dir.exists():
                mkv_files = [(f, None) for f in sorted(rip_dir.glob("*.mkv"))]
                if mkv_files:
                    ep_map = {str(rip.resolve()): ep for rip, ep in ripped_episodes}
                    manifest_files = []
                    for f, _ in mkv_files:
                        ep = ep_map.get(str(f.resolve()))
                        manifest_files.append((f, ep))
                    write_rip_manifest(rip_dir, manifest_files)
                rip_size = sum(f.stat().st_size for f in rip_dir.rglob("*") if f.is_file()) / (1024 ** 3)
                state.log.info(f"Raw rips preserved in _rips/ ({rip_size:.1f} GB). Use --cleanup to remove.")

        total_done = len(encode_results)
        final = f"{total_done} episodes"

    jellyfin_scan_library()

    summary = []
    summary.append("=" * 60)
    summary.append("PIPELINE COMPLETE!")
    summary.append(f"  Result: {final}")
    if meta:
        genres = ', '.join(meta.get('genres', []))
        creators = ', '.join(meta.get('directors', []))
        summary.append(f"  Genres: {genres}")
        if creators:
            label = 'Created by' if media_type == 'tv' else 'Director(s)'
            summary.append(f"  {label}: {creators}")
    summary.append(f"  Time: {datetime.now():%Y-%m-%d %H:%M:%S}")
    summary.append("=" * 60)

    state.log.info("")
    for line in summary:
        state.log.info(line)

    state._pipeline_summary = summary

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
# WATCH MODE
# ============================================================================

def watch_mode():
    """Poll for disc insertion and automatically start the pipeline (legacy)."""
    from ripper.helpers import disc_is_inserted

    state.log.info("Watch mode active. Waiting for disc insertion...")
    state.log.info(f"Polling every {CONFIG['poll_interval']} seconds.")
    state.log.info("Press Ctrl+C to stop.\n")

    was_inserted = disc_is_inserted()

    while True:
        try:
            is_inserted = disc_is_inserted()

            if is_inserted and not was_inserted:
                state.log.info("Disc detected! Starting pipeline...")
                time.sleep(5)
                run_pipeline()

            was_inserted = is_inserted
            time.sleep(CONFIG["poll_interval"])

        except KeyboardInterrupt:
            state.log.info("\nWatch mode stopped.")
            break


# ============================================================================
# CONTINUOUS WATCH MODE
# ============================================================================

def _encoder_worker(encode_queue, tui):
    """Long-lived encoder thread: pulls EncodeJobs, encodes, organizes, writes metadata."""
    while True:
        job = encode_queue.get()
        if job is None:
            # Sentinel — time to shut down
            break
        if state.is_shutdown_requested():
            encode_queue.task_done()
            continue

        try:
            # Restore config snapshot so this encode uses the right settings
            restore_encoder_config(job.encoder_config)

            if tui and tui.enabled:
                tui.update_encode(0.0, task=job.display_label)

            state.log.info(f"[ENCODE] Starting: {job.display_label}")

            job_raw_size = job.rip_mkv.stat().st_size if job.rip_mkv.exists() else 0
            job_encode_start = time.time()
            final = None

            if job.job_type == "movie":
                encoded = compress_mkv(job.rip_mkv, job.title_name)
                if not encoded:
                    state.log.error(f"[ENCODE] Failed: {job.display_label}")
                    encode_queue.task_done()
                    continue

                final = organize_file(encoded, job.title_name, job.year)

                if job.metadata:
                    folder_name = sanitize_filename(
                        f"{job.title_name} ({job.year})" if job.year else job.title_name
                    )
                    write_nfo(job.metadata, final.parent, folder_name)
                    download_artwork(job.metadata, final.parent)

                state.log.info(f"[ENCODE] Done: {final.name}")

            elif job.job_type == "tv_episode":
                ep_tag = f"S{job.season_num:02d}E{job.episode_num:02d}"
                encode_name = f"{sanitize_filename(job.title_name)} - {ep_tag}"

                encoded = compress_mkv(job.rip_mkv, encode_name,
                                       output_dir=CONFIG["rip_dir"])
                if not encoded:
                    state.log.error(f"[ENCODE] Failed: {ep_tag}")
                    encode_queue.task_done()
                    continue

                final = organize_tv_episode(
                    encoded, job.title_name, job.year,
                    job.season_num, job.episode_num, job.episode_name,
                )
                write_episode_nfo(
                    job.episode_info, final, job.title_name,
                    job.season_num, job.episode_num,
                )
                state.log.info(f"[ENCODE] Done: {ep_tag} → {final.name}")

            if tui and tui.enabled:
                tui.update_encode(100.0)
                tui.log(f"Encode complete: {job.display_label}")

            # Write history record
            if final and final.exists():
                job_encode_elapsed = time.time() - job_encode_start
                job_enc_gb = final.stat().st_size / (1024 ** 3)
                job_raw_gb = job_raw_size / (1024 ** 3) if job_raw_size else None
                job_probe = probe_media_file(final)
                src = job.source_info or {}
                h_rec = {
                    "title": job.title_name + (f" S{job.season_num:02d}E{job.episode_num:02d}" if job.job_type == "tv_episode" else ""),
                    "year": job.year,
                    "media_type": job.job_type.replace("tv_episode", "tv"),
                    "disc_type": src.get("disc_type"),
                    "resolution": f"{src.get('width', '')}x{src.get('height', '')}" if src.get("width") else None,
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "rip_time_min": None,
                    "encode_time_min": int(job_encode_elapsed // 60),
                    "raw_size_gb": round(job_raw_gb, 1) if job_raw_gb else None,
                    "encoded_size_gb": round(job_enc_gb, 1),
                    "compression_pct": round((1 - job_enc_gb / job_raw_gb) * 100) if job_raw_gb and job_raw_gb > 0 else None,
                    "video_codec": job_probe.get("video_codec", "").upper() + (" " + job_probe.get("video_profile", "")).rstrip() if job_probe else None,
                    "hdr": job_probe.get("hdr") if job_probe else None,
                    "audio": " + ".join(
                        t["codec"].upper() + (" " + t["layout"] if t["layout"] else f" {t['channels']}ch")
                        for t in job_probe.get("audio_tracks", [])
                    ) if job_probe else None,
                    "encoder": job.encoder_config.get("encoder"),
                    "quality_rf": job.encoder_config.get("quality_rf"),
                    "episodes": 1 if job.job_type == "tv_episode" else None,
                }
                try:
                    write_history_record(h_rec)
                except Exception as e:
                    state.log.warning(f"Failed to write history record: {e}")

            jellyfin_scan_library(quiet=True)

        except Exception as e:
            state.log.error(f"[ENCODE] Error on {job.display_label}: {e}")
        finally:
            encode_queue.task_done()


def _rip_tv_to_queue(title_name, year, season_num, start_episode,
                     episodes_info, metadata, encoder_snap, encode_queue, tui):
    """Rip TV episodes one at a time and push each to the encode queue."""
    makemkv = CONFIG["makemkv_bin"]
    min_length = CONFIG["min_episode_length"]

    rip_out = Path(CONFIG["rip_dir"]) / sanitize_filename(f"{title_name}_S{season_num:02d}")
    rip_out.mkdir(parents=True, exist_ok=True)

    state.log.info("Scanning disc for TV episodes...")
    scan = run_cmd([makemkv, "-r", "--minlength=0", "info", "disc:0"], timeout=300)
    if scan is None or scan.returncode != 0:
        state.log.error("Failed to scan disc.")
        return False

    source_info = detect_source_format(scan.stdout)
    auto_tune_for_source(source_info)
    # Re-snapshot after auto_tune since source may differ from previous disc
    encoder_snap_fresh = snapshot_encoder_config()

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
        state.log.error("No titles found on disc.")
        return False

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
        state.log.info(f"  Title {tid}: {name} — {duration_str} ({size_gb:.1f} GB)")

        if seconds >= min_length:
            episode_candidates.append((tid, seconds, size_bytes, name))

    if not episode_candidates:
        state.log.error(f"No titles found longer than {min_length // 60} minutes.")
        return False

    episode_candidates.sort(key=lambda x: x[0])

    # Filter out "play all" titles
    if len(episode_candidates) > 2:
        durations = [c[1] for c in episode_candidates]
        avg_duration = sum(durations) / len(durations)
        filtered = [c for c in episode_candidates if c[1] < avg_duration * 1.8]
        if len(filtered) >= 2:
            removed = len(episode_candidates) - len(filtered)
            if removed > 0:
                state.log.info(f"  Filtered out {removed} 'play all' title(s)")
            episode_candidates = filtered

    state.log.info(f"  Found {len(episode_candidates)} episodes to rip")

    # Build episode name/info maps
    ep_name_map = {}
    ep_info_map = {}
    if episodes_info:
        for ei in episodes_info:
            ep_name_map[ei.get("episode_number")] = ei.get("name", "")
            ep_info_map[ei.get("episode_number")] = ei

    # Populate TUI queue
    if tui and tui.enabled:
        for idx in range(len(episode_candidates)):
            ep_num = start_episode + idx
            ep_tag = f"S{season_num:02d}E{ep_num:02d}"
            ep_name = ep_name_map.get(ep_num, "")
            label = f"{ep_tag} — {ep_name}" if ep_name else ep_tag
            tui.add_queue_item(ep_tag, label)

    rip_count = 0
    for idx, (tid, seconds, _size_bytes, _name) in enumerate(episode_candidates):
        if state.is_shutdown_requested():
            state.log.info("  Stopping — Ctrl+C received.")
            break

        ep_num = start_episode + idx
        ep_name = ep_name_map.get(ep_num, "")
        ep_tag = f"S{season_num:02d}E{ep_num:02d}"
        display = f"{ep_tag}"
        if ep_name:
            display += f" — {ep_name}"

        state.log.info(f"  Ripping {display} (title {tid}, {seconds // 60} min)...")

        if tui and tui.enabled:
            tui.update_queue_item(ep_tag, "ripping")
            tui.update_rip(0.0, task=display)

        returncode, _stderr = run_cmd_progress(
            [
                makemkv, "-r", "mkv", "disc:0",
                str(tid), str(rip_out),
                "--minlength=0", "--progress=-stdout",
            ],
            timeout=3600,
            progress_handler=makemkv_progress,
        )
        if not (state.tui and state.tui.enabled):
            print()

        if returncode is None or returncode != 0:
            state.log.error(f"  Failed to rip title {tid}")
            if tui and tui.enabled:
                tui.update_queue_item(ep_tag, "failed")
            continue

        if tui and tui.enabled:
            tui.update_queue_item(ep_tag, "ripped")

        mkv_files = sorted(rip_out.glob("*.mkv"), key=lambda f: f.stat().st_mtime)
        if mkv_files:
            latest = mkv_files[-1]
            size_gb = latest.stat().st_size / (1024 ** 3)
            state.log.info(f"  Ripped: {latest.name} ({size_gb:.1f} GB) → queued for encode")
            rip_count += 1

            job = EncodeJob(
                job_type="tv_episode",
                title_name=title_name,
                year=year,
                rip_mkv=latest,
                encoder_config=encoder_snap_fresh,
                metadata=metadata,
                source_info=source_info,
                season_num=season_num,
                episode_num=ep_num,
                episode_name=ep_name,
                episode_info=ep_info_map.get(ep_num),
                display_label=f"{title_name} {ep_tag}",
                queue_id=ep_tag,
            )
            encode_queue.put(job)

    if tui and tui.enabled:
        tui.update_rip(100.0, task="All episodes ripped")

    state.log.info(f"  Ripped {rip_count} episodes total")
    return rip_count > 0


def watch_mode_continuous():
    """Persistent watch mode: TUI stays open, rip → eject → encode in background."""
    from ripper.helpers import disc_is_inserted

    ensure_dirs()
    if not check_external_drive():
        return

    # Skip check_drive() — watch mode waits for discs, so no disc is expected yet

    state.log.info("Continuous watch mode starting...")
    state.log.info(f"Polling every {CONFIG['poll_interval']} seconds.")
    state.log.info("Press Ctrl+C to stop.\n")

    # Create and start TUI in watch mode
    if sys.stdout.isatty():
        tui = RipperTUI()
        tui.set_watch_mode(True)
        tui.start()
        state.tui = tui
    else:
        tui = None

    # Encode queue and worker thread
    encode_q = queue.Queue()

    def _tracked_encoder_worker():
        """Wrapper that updates queue status as jobs complete."""
        while True:
            job = encode_q.get()
            if job is None:
                break
            if state.is_shutdown_requested():
                encode_q.task_done()
                continue

            try:
                restore_encoder_config(job.encoder_config)

                if tui and tui.enabled:
                    tui.update_queue_item(job.queue_id, "encoding")
                    tui.update_encode(0.0, task=job.display_label)

                state.log.info(f"[ENCODE] Starting: {job.display_label}")

                wm_raw_size = job.rip_mkv.stat().st_size if job.rip_mkv.exists() else 0
                wm_encode_start = time.time()
                final = None

                if job.job_type == "movie":
                    encoded = compress_mkv(job.rip_mkv, job.title_name)
                    if not encoded:
                        state.log.error(f"[ENCODE] Failed: {job.display_label}")
                        encode_q.task_done()
                        continue

                    final = organize_file(encoded, job.title_name, job.year)

                    if job.metadata:
                        folder_name = sanitize_filename(
                            f"{job.title_name} ({job.year})" if job.year else job.title_name
                        )
                        write_nfo(job.metadata, final.parent, folder_name)
                        download_artwork(job.metadata, final.parent)

                    rip_size = job.rip_mkv.stat().st_size / (1024 ** 3) if job.rip_mkv.exists() else 0
                    state.log.info(f"[ENCODE] Done: {final.name}")
                    state.log.info(f"  Raw rip preserved ({rip_size:.1f} GB). Use --cleanup to remove.")

                elif job.job_type == "tv_episode":
                    ep_tag = f"S{job.season_num:02d}E{job.episode_num:02d}"
                    encode_name = f"{sanitize_filename(job.title_name)} - {ep_tag}"

                    encoded = compress_mkv(job.rip_mkv, encode_name,
                                           output_dir=CONFIG["rip_dir"])
                    if not encoded:
                        state.log.error(f"[ENCODE] Failed: {ep_tag}")
                        if tui and tui.enabled:
                            tui.update_queue_item(job.queue_id, "failed")
                        encode_q.task_done()
                        continue

                    final = organize_tv_episode(
                        encoded, job.title_name, job.year,
                        job.season_num, job.episode_num, job.episode_name,
                    )
                    write_episode_nfo(
                        job.episode_info, final, job.title_name,
                        job.season_num, job.episode_num,
                    )
                    state.log.info(f"[ENCODE] Done: {ep_tag} → {final.name}")

                if tui and tui.enabled:
                    tui.update_queue_item(job.queue_id, "done")
                    tui.update_encode(100.0)
                    tui.log(f"Encode complete: {job.display_label}")

                # Write history record
                if final and final.exists():
                    wm_encode_elapsed = time.time() - wm_encode_start
                    wm_enc_gb = final.stat().st_size / (1024 ** 3)
                    wm_raw_gb = wm_raw_size / (1024 ** 3) if wm_raw_size else None
                    wm_probe = probe_media_file(final)
                    wm_src = job.source_info or {}
                    wm_rec = {
                        "title": job.title_name + (f" S{job.season_num:02d}E{job.episode_num:02d}" if job.job_type == "tv_episode" else ""),
                        "year": job.year,
                        "media_type": job.job_type.replace("tv_episode", "tv"),
                        "disc_type": wm_src.get("disc_type"),
                        "resolution": f"{wm_src.get('width', '')}x{wm_src.get('height', '')}" if wm_src.get("width") else None,
                        "date": datetime.now().isoformat(timespec="seconds"),
                        "rip_time_min": None,
                        "encode_time_min": int(wm_encode_elapsed // 60),
                        "raw_size_gb": round(wm_raw_gb, 1) if wm_raw_gb else None,
                        "encoded_size_gb": round(wm_enc_gb, 1),
                        "compression_pct": round((1 - wm_enc_gb / wm_raw_gb) * 100) if wm_raw_gb and wm_raw_gb > 0 else None,
                        "video_codec": wm_probe.get("video_codec", "").upper() + (" " + wm_probe.get("video_profile", "")).rstrip() if wm_probe else None,
                        "hdr": wm_probe.get("hdr") if wm_probe else None,
                        "audio": " + ".join(
                            t["codec"].upper() + (" " + t["layout"] if t["layout"] else f" {t['channels']}ch")
                            for t in wm_probe.get("audio_tracks", [])
                        ) if wm_probe else None,
                        "encoder": job.encoder_config.get("encoder"),
                        "quality_rf": job.encoder_config.get("quality_rf"),
                        "episodes": 1 if job.job_type == "tv_episode" else None,
                    }
                    try:
                        write_history_record(wm_rec)
                    except Exception as e:
                        state.log.warning(f"Failed to write history record: {e}")

                jellyfin_scan_library(quiet=True)

            except Exception as e:
                state.log.error(f"[ENCODE] Error on {job.display_label}: {e}")
            finally:
                encode_q.task_done()

    encoder_thread = threading.Thread(target=_tracked_encoder_worker, daemon=True)
    encoder_thread.start()

    # Start as False so a disc already in the drive is detected immediately
    was_inserted = False

    try:
        while not state.is_shutdown_requested():
            is_inserted = disc_is_inserted()

            if is_inserted and not was_inserted:
                # New disc detected
                state.log.info("Disc detected! Waiting for drive to settle...")
                if tui and tui.enabled:
                    tui.set_disc_status("detecting")
                time.sleep(5)

                if state.is_shutdown_requested():
                    break

                # Pause TUI for interactive metadata prompts
                if tui and tui.enabled:
                    tui.pause()

                disc_info = get_disc_metadata()

                if tui and tui.enabled:
                    tui.resume()

                title_name = disc_info["title"]
                year = disc_info.get("year")
                media_type = disc_info.get("media_type", "movie")
                meta = disc_info.get("metadata")

                if not title_name:
                    state.log.error("No title determined. Skipping this disc.")
                    was_inserted = is_inserted
                    if tui and tui.enabled:
                        tui.set_disc_status("waiting")
                    continue

                # Update TUI
                if tui and tui.enabled:
                    tui.set_metadata(title_name, year, media_type)
                    poster_path = meta.get("poster_path") if meta else None
                    if poster_path:
                        art = poster_to_ascii(poster_path)
                        if art:
                            tui.set_poster(art)
                    tui.set_disc_status("ripping", title_name)
                    tui.start_disc_timer()

                state.log.info(f"Starting pipeline for [{media_type.upper()}]: {title_name}"
                               + (f" ({year})" if year else ""))

                if media_type == "movie":
                    # Rip movie
                    job_id = f"movie-{int(time.time())}"
                    display_label = f"{title_name}" + (f" ({year})" if year else "")
                    if tui and tui.enabled:
                        tui.add_queue_item(job_id, display_label, status="ripping")
                        tui.update_rip(0.0, task="Main feature")
                    rip_mkv, source_info = rip_disc(title_name)

                    if not rip_mkv or state.is_shutdown_requested():
                        if not rip_mkv:
                            state.log.error("Rip failed. Ejecting and resuming watch.")
                        if tui and tui.enabled:
                            tui.update_queue_item(job_id, "failed")
                        eject_disc()
                        was_inserted = False
                        if tui and tui.enabled:
                            tui.set_disc_status("waiting")
                            tui.set_poster(None)
                            tui.poster_art = None
                            tui._poster_panel = None
                        continue

                    if tui and tui.enabled:
                        tui.update_queue_item(job_id, "ripped")
                        tui.update_rip(100.0)
                        tui.log(f"Rip complete: {rip_mkv.name}")

                    # Snapshot config AFTER rip (auto_tune ran during rip)
                    encoder_snap = snapshot_encoder_config()

                    # Eject immediately
                    eject_disc()
                    was_inserted = False
                    if tui and tui.enabled:
                        tui.set_disc_status("ejected")

                    write_rip_manifest(rip_mkv.parent, [(rip_mkv, None)])

                    # Push encode job
                    job = EncodeJob(
                        job_type="movie",
                        title_name=title_name,
                        year=year,
                        rip_mkv=rip_mkv,
                        encoder_config=encoder_snap,
                        metadata=meta,
                        source_info=source_info or {},
                        display_label=display_label,
                        queue_id=job_id,
                    )
                    encode_q.put(job)
                    state.log.info(f"Queued for encode: {job.display_label}")

                else:
                    # TV show
                    season_num = disc_info.get("season") or 1
                    ep_start = disc_info.get("start_episode") or 1

                    episodes_info = []
                    if meta and meta.get("tmdb_id"):
                        episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], season_num)

                    # Snapshot baseline config before rip
                    encoder_snap = snapshot_encoder_config()

                    # Rip episodes one at a time, pushing to queue
                    # _rip_tv_to_queue handles its own auto_tune and snapshots
                    ok = _rip_tv_to_queue(
                        title_name, year, season_num, ep_start,
                        episodes_info, meta, encoder_snap, encode_q, tui,
                    )

                    # Write show-level metadata
                    if ok and meta:
                        show_dir = Path(CONFIG["tv_encode_dir"]) / sanitize_filename(
                            f"{title_name} ({year})" if year else title_name
                        )
                        show_dir.mkdir(parents=True, exist_ok=True)
                        write_tv_nfo(meta, show_dir, title_name, season_num)
                        download_artwork(meta, show_dir)

                    eject_disc()
                    was_inserted = False
                    if tui and tui.enabled:
                        tui.set_disc_status("ejected")

                # Reset for next disc
                if tui and tui.enabled:
                    tui.set_disc_status("waiting")
                    tui.set_poster(None)
                    tui.poster_art = None
                    tui._poster_panel = None
                    tui.title = ""
                    tui.year = ""
                    tui.rip_pct = 0.0
                    tui.rip_task = ""
                    tui.clear_queue()

            was_inserted = is_inserted
            time.sleep(CONFIG["poll_interval"])

    except KeyboardInterrupt:
        pass

    # Graceful shutdown
    state.log.info("\nWatch mode stopping...")

    # Send sentinel to encoder thread
    encode_q.put(None)
    encoder_thread.join(timeout=30)

    if tui:
        tui.stop()
        state.tui = None

    state.log.info("Watch mode stopped.")
