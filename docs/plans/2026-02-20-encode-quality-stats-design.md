# Post-Encode Quality Stats

## Summary

After each file is encoded, run `ffprobe` on the output MKV and log a quality summary showing video specs, HDR info, audio tracks, and size comparison vs the raw rip.

## Probe tool

Requires `ffprobe` (from ffmpeg). Discovered at startup via `_find_binary()` in config.py. If not found, stats are skipped with a log warning — encoding still works.

## New functions in `ripper/media.py`

### `probe_media_file(path) -> dict | None`

Calls `ffprobe -v quiet -print_format json -show_format -show_streams <file>` and parses the JSON output.

Returns:
```python
{
    "video_codec": "hevc",
    "video_profile": "Main 10",
    "resolution": (3840, 2160),
    "frame_rate": "23.976",
    "hdr": "HDR10" | "Dolby Vision" | "HLG" | None,
    "color_primaries": "bt2020",
    "audio_tracks": [
        {"codec": "truehd", "channels": 8, "layout": "7.1", "language": "eng"},
        {"codec": "aac", "channels": 2, "layout": "stereo", "language": "eng"},
    ],
    "file_size_bytes": 19771093504,
    "duration_seconds": 8160.5,
    "bitrate_kbps": 19384,
}
```

Returns `None` if ffprobe is not available or fails.

### `log_encode_stats(probe_result, raw_size_bytes=None)`

Formats and logs the stats summary. Example output:

```
  Encode stats: The Matrix (1999).mkv
    Video:  3840x2160 @ 23.976 fps, HEVC Main 10
    HDR:    HDR10 (BT.2020)
    Audio:  TrueHD 7.1 + AAC Stereo
    Size:   18.4 GB (was 54.2 GB — 66% smaller)
```

## Integration points

- **Movie pipeline** (`pipeline.py`): Called after `compress_mkv()` returns, before `organize_file()`. Raw rip size captured from input file.
- **TV pipeline** (`pipeline.py`): Called after each episode encodes, before `organize_tv_episode()`.
- **TUI**: Stats appear in the existing scrolling log area — no new panel needed.

## Config changes

- `ffprobe_path` added to config, discovered by `_find_binary("ffprobe")` during `_detect_defaults()`.

## What we extract from ffprobe JSON

- **Video**: `codec_name`, `profile`, `width`/`height`, `r_frame_rate`, `color_primaries`, `color_transfer`
- **HDR**: `color_transfer == "smpte2084"` = HDR10, `"arib-std-b67"` = HLG. Dolby Vision from `side_data_list` entries.
- **Audio**: Each audio stream's `codec_name`, `channels`, `channel_layout`, `tags.language`
- **File size**: `format.size` or `os.path.getsize()`
- **Bitrate**: `format.bit_rate`

## Graceful degradation

If ffprobe is missing or the probe fails, log a single warning and continue. Encoding is never blocked by stats.
