"""Tests for ripper.media."""

import logging

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
