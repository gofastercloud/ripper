"""Tests for ripper.cleanup."""

import json
import logging

from ripper import state
from ripper.cleanup import _parse_folder_title_year, verify_rip_manifest, write_rip_manifest


def _ensure_log():
    if state.log is None:
        state.log = logging.getLogger("test")


class TestRipManifest:
    def test_write_verify_rip_manifest(self, tmp_path):
        """Write manifest → verify returns valid entries."""
        _ensure_log()
        # Create a fake rip file
        rip_file = tmp_path / "title_t00.mkv"
        rip_file.write_bytes(b"fake mkv data" * 100)

        write_rip_manifest(tmp_path, [(rip_file, None)])

        manifest_path = tmp_path / "rip_manifest.json"
        assert manifest_path.exists()

        with open(manifest_path) as f:
            manifest = json.load(f)
        assert len(manifest["files"]) == 1
        assert manifest["files"][0]["filename"] == "title_t00.mkv"

        # Verify should find the file valid
        valid = verify_rip_manifest(tmp_path)
        assert valid is not None
        assert len(valid) == 1
        assert valid[0][0] == rip_file


class TestParseFolderTitleYear:
    def test_parse_folder_title_year(self):
        """Title/year extraction from folder names."""
        title, year = _parse_folder_title_year("The Matrix (1999)")
        assert title == "The Matrix"
        assert year == "1999"

        title, year = _parse_folder_title_year("No Year Movie")
        assert title == "No Year Movie"
        assert year is None

        title, year = _parse_folder_title_year("Blade Runner 2049 (2017)")
        assert title == "Blade Runner 2049"
        assert year == "2017"
