"""Tests for ripper.config."""

import json
import os
from unittest.mock import patch

from ripper import state
from ripper.config import _derive_paths, load_config, save_config


def _reset_config():
    """Reset CONFIG to defaults for test isolation."""
    from ripper.state import CONFIG
    defaults = {
        "output_base": None, "rip_dir": None, "encode_dir": None,
        "tv_encode_dir": None, "log_dir": None, "metadata_cache": None,
        "makemkv_bin": None, "min_title_length": 1200, "min_episode_length": 600,
        "handbrake_bin": None, "encoder": "vt_h265_10bit", "quality_rf": 55,
        "encoder_preset": "quality", "encoder_tune": None, "encoder_profile": "main10",
        "encoder_level": "auto", "hq_mode": False, "deinterlace": False,
        "output_format": "mkv", "viewing_profile": "mixed",
        "quality_rf_dvd": 60, "quality_rf_bd": 52, "quality_rf_uhd": 50,
        "audio_mode": "copy,aac", "poll_interval": 10,
        "tmdb_api_key": "", "jellyfin_url": "http://localhost:8096",
        "jellyfin_api_key": "",
    }
    CONFIG.clear()
    CONFIG.update(defaults)


class TestLoadConfig:
    def test_load_config_merges_with_defaults(self, tmp_path):
        """Config loading merges user values with defaults."""
        _reset_config()
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "output_base": "/tmp/test_media",
            "quality_rf": 42,
        }))
        load_config(str(config_file))
        assert state.CONFIG["output_base"] == "/tmp/test_media"
        assert state.CONFIG["quality_rf"] == 42
        # Default value preserved
        assert state.CONFIG["encoder"] == "vt_h265_10bit"

    def test_load_config_env_var_overrides(self, tmp_path):
        """Env vars override config file values."""
        _reset_config()
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"tmdb_api_key": "from_file"}))
        with patch.dict(os.environ, {"TMDB_API_KEY": "from_env"}):
            load_config(str(config_file))
        assert state.CONFIG["tmdb_api_key"] == "from_env"

    def test_save_config_roundtrip(self, tmp_path):
        """Save then load produces same values."""
        _reset_config()
        state.CONFIG["output_base"] = "/tmp/roundtrip"
        state.CONFIG["quality_rf"] = 99
        _derive_paths("/tmp/roundtrip")
        config_file = tmp_path / "config.json"
        save_config(str(config_file))

        _reset_config()
        load_config(str(config_file))
        assert state.CONFIG["output_base"] == "/tmp/roundtrip"
        assert state.CONFIG["quality_rf"] == 99

    def test_derive_paths(self):
        """_derive_paths sets all subdirs correctly."""
        _reset_config()
        _derive_paths("/media/root")
        assert state.CONFIG["rip_dir"] == "/media/root/_rips"
        assert state.CONFIG["encode_dir"] == "/media/root/Movies"
        assert state.CONFIG["tv_encode_dir"] == "/media/root/Shows"
        assert state.CONFIG["log_dir"] == "/media/root/_logs"
        assert state.CONFIG["metadata_cache"] == "/media/root/_cache/metadata.json"

    def test_load_config_bad_json_no_crash(self, tmp_path):
        """Invalid JSON logs warning, doesn't raise."""
        _reset_config()
        config_file = tmp_path / "config.json"
        config_file.write_text("{this is not valid json!!!")
        # Should not raise
        load_config(str(config_file))
        # Defaults still intact
        assert state.CONFIG["encoder"] == "vt_h265_10bit"


class TestDetectDefaults:
    def test_detect_defaults_finds_ffprobe(self):
        """_detect_defaults sets ffprobe_bin when ffprobe is available."""
        from ripper import state
        state.CONFIG["ffprobe_bin"] = ""
        from unittest.mock import patch

        from ripper.config import _detect_defaults
        with patch("ripper.config._find_binary", return_value="/usr/local/bin/ffprobe"):
            _detect_defaults()
        assert state.CONFIG["ffprobe_bin"] == "/usr/local/bin/ffprobe"
