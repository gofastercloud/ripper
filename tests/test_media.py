"""Tests for ripper.media."""

import json
import logging
from unittest.mock import MagicMock, patch

from ripper import state
from ripper.media import auto_tune_for_source, detect_source_format


def _ensure_log():
    if state.log is None:
        state.log = logging.getLogger("test")


class TestDetectSourceFormat:
    def test_detect_source_format_uhd(self):
        """UHD disc scan output → disc_type == 'uhd'."""
        _ensure_log()
        scan = (
            'CINFO:1,6209,"UHD Blu-ray disc"\n'
            'SINFO:0,0,19,0,"3840x2160"\n'
        )
        info = detect_source_format(scan)
        assert info["disc_type"] == "uhd"
        assert info["resolution"] == (3840, 2160)
        assert info["interlaced"] is False

    def test_detect_source_format_dvd(self):
        """DVD scan output → disc_type == 'dvd', interlaced."""
        _ensure_log()
        scan = (
            'CINFO:1,6210,"DVD disc"\n'
            'SINFO:0,0,19,0,"720x480"\n'
        )
        info = detect_source_format(scan)
        assert info["disc_type"] == "dvd"
        assert info["resolution"] == (720, 480)
        assert info["interlaced"] is True  # DVDs always flagged as interlaced

    def test_detect_source_format_bluray(self):
        """BD scan output → disc_type == 'bluray'."""
        _ensure_log()
        scan = (
            'CINFO:1,6209,"Blu-ray disc"\n'
            'SINFO:0,0,19,0,"1920x1080"\n'
        )
        info = detect_source_format(scan)
        assert info["disc_type"] == "bluray"
        assert info["resolution"] == (1920, 1080)
        assert info["interlaced"] is False


class TestAutoTune:
    def test_auto_tune_dvd_sets_deinterlace(self):
        """DVD source → deinterlace enabled."""
        _ensure_log()
        # Reset relevant config
        state.CONFIG["deinterlace"] = False
        state.CONFIG["_user_set_hq"] = False
        state.CONFIG["_user_set_quality"] = False

        source_info = {"disc_type": "dvd", "resolution": (720, 480), "interlaced": True}
        auto_tune_for_source(source_info)

        assert state.CONFIG["deinterlace"] is True
        assert state.CONFIG["quality_rf"] == state.CONFIG["quality_rf_dvd"]


class TestProbeMediaFile:
    def test_probe_media_file_parses_ffprobe_json(self):
        """probe_media_file extracts video, audio, HDR info from ffprobe output."""
        _ensure_log()
        fake_output = json.dumps({
            "format": {
                "size": "19771093504",
                "bit_rate": "19384000",
                "duration": "8160.500000",
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "width": 3840,
                    "height": 2160,
                    "r_frame_rate": "24000/1001",
                    "color_primaries": "bt2020",
                    "color_transfer": "smpte2084",
                    "side_data_list": [],
                },
                {
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "channels": 8,
                    "channel_layout": "7.1",
                    "tags": {"language": "eng"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "tags": {"language": "eng"},
                },
            ],
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output

        state.CONFIG["ffprobe_bin"] = "/usr/local/bin/ffprobe"
        with patch("ripper.media.subprocess.run", return_value=mock_result):
            from ripper.media import probe_media_file
            result = probe_media_file("/fake/movie.mkv")

        assert result is not None
        assert result["video_codec"] == "hevc"
        assert result["video_profile"] == "Main 10"
        assert result["resolution"] == (3840, 2160)
        assert result["hdr"] == "HDR10"
        assert len(result["audio_tracks"]) == 2
        assert result["audio_tracks"][0]["codec"] == "truehd"
        assert result["audio_tracks"][0]["channels"] == 8
        assert result["file_size_bytes"] == 19771093504

    def test_probe_media_file_returns_none_without_ffprobe(self):
        """Returns None when ffprobe_bin is not configured."""
        _ensure_log()
        state.CONFIG["ffprobe_bin"] = ""
        from ripper.media import probe_media_file
        result = probe_media_file("/fake/movie.mkv")
        assert result is None

    def test_probe_media_file_detects_dolby_vision(self):
        """Dolby Vision detected from side_data_list."""
        _ensure_log()
        fake_output = json.dumps({
            "format": {"size": "1000000", "bit_rate": "5000000", "duration": "100.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "width": 3840,
                    "height": 2160,
                    "r_frame_rate": "24/1",
                    "color_primaries": "bt2020",
                    "color_transfer": "smpte2084",
                    "side_data_list": [
                        {"side_data_type": "DOVI configuration record"}
                    ],
                },
            ],
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output

        state.CONFIG["ffprobe_bin"] = "/usr/local/bin/ffprobe"
        with patch("ripper.media.subprocess.run", return_value=mock_result):
            from ripper.media import probe_media_file
            result = probe_media_file("/fake/movie.mkv")

        assert result["hdr"] == "Dolby Vision"


class TestLogEncodeStats:
    def test_log_encode_stats_formats_output(self):
        """log_encode_stats logs video, HDR, audio, and size info."""
        _ensure_log()
        probe_result = {
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "resolution": (3840, 2160),
            "frame_rate": "23.976",
            "hdr": "HDR10",
            "color_primaries": "bt2020",
            "audio_tracks": [
                {"codec": "truehd", "channels": 8, "layout": "7.1", "language": "eng"},
                {"codec": "aac", "channels": 2, "layout": "stereo", "language": "eng"},
            ],
            "file_size_bytes": 19771093504,
            "duration_seconds": 8160.5,
            "bitrate_kbps": 19384,
        }
        from ripper.media import log_encode_stats
        # Should not raise; output goes to logger
        log_encode_stats(probe_result, raw_size_bytes=54 * 1024**3)

    def test_log_encode_stats_handles_no_raw_size(self):
        """log_encode_stats works without raw size comparison."""
        _ensure_log()
        probe_result = {
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "resolution": (1920, 1080),
            "frame_rate": "24.0",
            "hdr": None,
            "color_primaries": "bt709",
            "audio_tracks": [
                {"codec": "aac", "channels": 2, "layout": "stereo", "language": "eng"},
            ],
            "file_size_bytes": 2000000000,
            "duration_seconds": 5400.0,
            "bitrate_kbps": 3000,
        }
        from ripper.media import log_encode_stats
        log_encode_stats(probe_result)
