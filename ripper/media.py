"""MakeMKV/HandBrake progress parsing and source format detection."""

import json
import re
import subprocess

from ripper import state

CONFIG = state.CONFIG


def makemkv_progress(line):
    """Parse MakeMKV robot-mode progress lines and display them."""
    if line.startswith("PRGV:"):
        parts = line[5:].split(",")
        if len(parts) >= 3:
            current = int(parts[0])
            total = int(parts[2])
            if total > 0:
                pct = (current / total) * 100
                if state.tui and state.tui.enabled:
                    state.tui.update_rip(pct)
                else:
                    print(f"\r  Ripping: {pct:5.1f}% complete", end="", flush=True)
    elif line.startswith("PRGT:"):
        task = line.split(",")[-1].strip('"')
        if task:
            if not (state.tui and state.tui.enabled):
                print(f"\r  {task:<60}", end="", flush=True)
    elif line.startswith("MSG:"):
        parts = line.split(",", 4)
        if len(parts) >= 5:
            msg = parts[3].strip('"')
            state.log.info(f"  MakeMKV: {msg}")
            if state.tui and state.tui.enabled:
                if any(kw in line for kw in ["error", "Error", "fail", "Fail", "LibreDrive"]):
                    state.tui.log(msg)


def handbrake_progress(line):
    """Parse HandBrake CLI progress output."""
    if "Encoding:" in line and "%" in line:
        match = re.search(r'(\d+\.\d+)\s*%.*?ETA\s*(\d+h\d+m\d+s)', line)
        if match:
            pct = float(match.group(1))
            eta = match.group(2)
            if state.tui and state.tui.enabled:
                state.tui.update_encode(pct, eta=eta)
            else:
                print(f"\r  Encoding: {pct:5.1f}% — ETA {eta}  ", end="", flush=True)
        else:
            match = re.search(r'(\d+\.\d+)\s*%', line)
            if match:
                pct = float(match.group(1))
                if state.tui and state.tui.enabled:
                    state.tui.update_encode(pct)
                else:
                    print(f"\r  Encoding: {pct:5.1f}%  ", end="", flush=True)
    elif "work result" in line.lower() or "Muxing" in line:
        msg = line.strip()
        if state.tui and state.tui.enabled:
            state.tui.log(msg)
        else:
            print(f"\n  {msg}")


def detect_source_format(scan_output):
    """
    Detect the source format (DVD, Blu-ray, UHD) from MakeMKV scan output.

    Returns dict with:
      disc_type: "dvd" | "bluray" | "uhd" | "unknown"
      resolution: (width, height) or None
      interlaced: True/False
    """
    info = {"disc_type": "unknown", "resolution": None, "interlaced": False}
    if not scan_output:
        return info

    max_res_pixels = 0

    for line in scan_output.splitlines():
        if line.startswith("CINFO:1,"):
            lower = line.lower()
            if "dvd" in lower:
                info["disc_type"] = "dvd"
            elif "uhd" in lower or "4k" in lower:
                info["disc_type"] = "uhd"
            elif "blu-ray" in lower or "bd" in lower:
                info["disc_type"] = "bluray"

        if "SINFO:" in line:
            res_match = re.search(r'(\d{3,5})x(\d{3,5})', line)
            if res_match:
                w, h = int(res_match.group(1)), int(res_match.group(2))
                pixels = w * h
                if pixels > max_res_pixels:
                    max_res_pixels = pixels
                    info["resolution"] = (w, h)

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
        info["interlaced"] = True

    return info


def auto_tune_for_source(source_info):
    """
    Automatically adjust encoder settings based on source format.
    Uses per-source RF values from the viewing profile.
    Respects manual overrides: --hq forces software x265, --quality skips RF assignment.
    """
    disc_type = source_info.get("disc_type", "unknown")
    res = source_info.get("resolution")
    res_str = f"{res[0]}x{res[1]}" if res else "unknown"

    state.log.info(f"  Source: {disc_type.upper()} ({res_str})")

    if CONFIG.get("_user_set_hq"):
        state.log.info("  Encoder: user override (--hq)")
        if disc_type == "dvd":
            CONFIG["deinterlace"] = True
            state.log.info("  Deinterlace: auto-enabled for DVD source")
        return

    if not CONFIG.get("_user_set_quality"):
        if disc_type == "dvd":
            CONFIG["quality_rf"] = CONFIG["quality_rf_dvd"]
        elif disc_type == "bluray":
            CONFIG["quality_rf"] = CONFIG["quality_rf_bd"]
        elif disc_type == "uhd":
            CONFIG["quality_rf"] = CONFIG["quality_rf_uhd"]
    else:
        state.log.info("  RF: user override (--quality)")

    CONFIG["encoder"] = "vt_h265_10bit"
    CONFIG["encoder_preset"] = "quality"
    CONFIG["encoder_level"] = "auto"
    CONFIG["hq_mode"] = False

    if disc_type == "dvd":
        CONFIG["deinterlace"] = True
        state.log.info(f"  Auto-tuned: DVD mode (VideoToolbox @ RF {CONFIG['quality_rf']}, deinterlace on)")
    elif disc_type == "bluray":
        CONFIG["deinterlace"] = False
        state.log.info(f"  Auto-tuned: Blu-ray mode (VideoToolbox @ RF {CONFIG['quality_rf']})")
    elif disc_type == "uhd":
        CONFIG["deinterlace"] = False
        state.log.info(f"  Auto-tuned: UHD mode (VideoToolbox @ RF {CONFIG['quality_rf']})")
        state.log.info("  HDR: automatic passthrough (HDR10/Dolby Vision preserved by HandBrake)")
    else:
        state.log.info("  Unknown source — using default settings")

    if state.tui and state.tui.enabled:
        is_hq = CONFIG.get("hq_mode", False)
        enc_label = f"x265 RF {CONFIG['quality_rf']}" if is_hq else f"VideoToolbox RF {CONFIG['quality_rf']}"
        state.tui.set_metadata(
            state.tui.title, state.tui.year, state.tui.media_type,
            source_format=f"{disc_type.upper()} {res_str}",
            encoder_mode=enc_label,
        )


def probe_media_file(file_path):
    """Run ffprobe on a file and return parsed media info, or None if unavailable."""
    ffprobe = CONFIG.get("ffprobe_bin", "")
    if not ffprobe:
        return None

    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(file_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            state.log.warning(f"ffprobe failed (exit {result.returncode})")
            return None

        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        state.log.warning(f"ffprobe error: {e}")
        return None

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    # Video
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return None

    # Frame rate
    r_frame_rate = video.get("r_frame_rate", "0/1")
    try:
        num, den = r_frame_rate.split("/")
        fps = round(int(num) / int(den), 3)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    frame_rate = str(fps)

    # HDR detection
    color_transfer = video.get("color_transfer", "")
    side_data = video.get("side_data_list", [])
    has_dovi = any("DOVI" in (sd.get("side_data_type", "").upper()) for sd in side_data)

    if has_dovi:
        hdr = "Dolby Vision"
    elif color_transfer == "smpte2084":
        hdr = "HDR10"
    elif color_transfer == "arib-std-b67":
        hdr = "HLG"
    else:
        hdr = None

    # Audio tracks
    audio_tracks = []
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        audio_tracks.append({
            "codec": s.get("codec_name", "unknown"),
            "channels": s.get("channels", 0),
            "layout": s.get("channel_layout", ""),
            "language": s.get("tags", {}).get("language", ""),
        })

    return {
        "video_codec": video.get("codec_name", "unknown"),
        "video_profile": video.get("profile", ""),
        "resolution": (video.get("width", 0), video.get("height", 0)),
        "frame_rate": frame_rate,
        "hdr": hdr,
        "color_primaries": video.get("color_primaries", ""),
        "audio_tracks": audio_tracks,
        "file_size_bytes": int(fmt.get("size", 0)),
        "duration_seconds": float(fmt.get("duration", 0)),
        "bitrate_kbps": int(fmt.get("bit_rate", 0)) // 1000,
    }


def log_encode_stats(probe_result, raw_size_bytes=None):
    """Log a human-readable summary of the encoded file's media properties."""
    if not probe_result:
        return

    res = probe_result["resolution"]
    video_line = f"{res[0]}x{res[1]} @ {probe_result['frame_rate']} fps, {probe_result['video_codec'].upper()}"
    if probe_result["video_profile"]:
        video_line += f" {probe_result['video_profile']}"

    state.log.info("  Encode stats:")
    state.log.info(f"    Video:  {video_line}")

    if probe_result["hdr"]:
        hdr_line = probe_result["hdr"]
        if probe_result["color_primaries"]:
            hdr_line += f" ({probe_result['color_primaries']})"
        state.log.info(f"    HDR:    {hdr_line}")

    if probe_result["audio_tracks"]:
        parts = []
        for t in probe_result["audio_tracks"]:
            desc = t["codec"].upper()
            if t["layout"]:
                desc += f" {t['layout']}"
            elif t["channels"]:
                desc += f" {t['channels']}ch"
            parts.append(desc)
        state.log.info(f"    Audio:  {' + '.join(parts)}")

    out_bytes = probe_result["file_size_bytes"]
    out_gb = out_bytes / (1024 ** 3)
    if raw_size_bytes and raw_size_bytes > 0:
        raw_gb = raw_size_bytes / (1024 ** 3)
        reduction = (1 - out_bytes / raw_size_bytes) * 100
        state.log.info(f"    Size:   {out_gb:.1f} GB (was {raw_gb:.1f} GB — {reduction:.0f}% smaller)")
    else:
        state.log.info(f"    Size:   {out_gb:.1f} GB")
