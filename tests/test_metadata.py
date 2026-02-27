"""Tests for ripper.metadata."""

import json
import logging
import xml.etree.ElementTree as ET

from ripper import state
from ripper.metadata import clean_disc_label, tmdb_search, write_nfo
from ripper.metadata import update_disc_map, get_disc_start_episode


def _ensure_log():
    if state.log is None:
        state.log = logging.getLogger("test")


class TestCleanDiscLabel:
    def test_clean_disc_label_strips_uhd(self):
        """BLADE_RUNNER_2049_4KUHD → Blade Runner 2049."""
        _ensure_log()
        label, season, disc = clean_disc_label("BLADE_RUNNER_2049_4KUHD")
        assert label == "Blade Runner 2049"
        assert season is None
        assert disc is None

    def test_clean_disc_label_extracts_season(self):
        """BREAKING_BAD_S02_D1 → title, season=2, disc=1."""
        _ensure_log()
        label, season, disc = clean_disc_label("BREAKING_BAD_S02_D1")
        assert "Breaking Bad" in label
        assert season == 2
        assert disc == 1


class TestTmdbSearch:
    def test_tmdb_search_no_key_returns_none(self):
        """Empty API key → None, no HTTP call."""
        _ensure_log()
        state.CONFIG["tmdb_api_key"] = ""
        result = tmdb_search("The Matrix")
        assert result is None


class TestWriteNfo:
    def test_write_nfo_valid_xml(self, tmp_path):
        """write_nfo produces parseable XML with correct tags."""
        _ensure_log()
        metadata = {
            "title": "The Matrix",
            "year": "1999",
            "tmdb_id": 603,
            "overview": "A computer hacker learns about the true nature of reality.",
            "original_title": "The Matrix",
            "tagline": "Welcome to the Real World.",
            "runtime": 136,
            "certification": "R",
            "vote_average": 8.2,
            "studio": "Warner Bros.",
            "genres": ["Action", "Sci-Fi"],
            "directors": ["Lana Wachowski"],
            "cast": [{"name": "Keanu Reeves", "role": "Neo"}],
            "poster_path": "/poster.jpg",
        }
        write_nfo(metadata, str(tmp_path), "The Matrix (1999)")

        nfo_path = tmp_path / "The Matrix (1999).nfo"
        assert nfo_path.exists()

        tree = ET.parse(nfo_path)
        root = tree.getroot()
        assert root.tag == "movie"
        assert root.find("title").text == "The Matrix"
        assert root.find("year").text == "1999"
        assert root.find("uniqueid").text == "603"
        assert root.find("genre").text == "Action"
        assert root.find("director").text == "Lana Wachowski"


class TestDiscMap:
    def test_update_disc_map_creates_entry(self, tmp_path, monkeypatch):
        """update_disc_map writes disc episode count into cache _disc_maps."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(cache_file))
        _ensure_log()

        update_disc_map("Breaking Bad", 1, 1, 4)

        data = json.loads(cache_file.read_text())
        assert data["_disc_maps"]["Breaking Bad::S01"]["1"] == 4

    def test_update_disc_map_overwrites_existing(self, tmp_path, monkeypatch):
        """update_disc_map overwrites a previous count for the same disc."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(cache_file))
        _ensure_log()

        update_disc_map("Breaking Bad", 1, 1, 3)
        update_disc_map("Breaking Bad", 1, 1, 4)

        data = json.loads(cache_file.read_text())
        assert data["_disc_maps"]["Breaking Bad::S01"]["1"] == 4

    def test_get_disc_start_episode_returns_correct_offset(self, tmp_path, monkeypatch):
        """Disc 2 start = sum of all disc 1 episodes + 1."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(cache_file))
        _ensure_log()

        update_disc_map("Breaking Bad", 1, 1, 4)

        result = get_disc_start_episode("Breaking Bad", 1, 2)
        assert result == 5

    def test_get_disc_start_episode_sums_multiple_discs(self, tmp_path, monkeypatch):
        """Disc 3 start = sum of discs 1 + 2 + 1."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(cache_file))
        _ensure_log()

        update_disc_map("Breaking Bad", 1, 1, 4)
        update_disc_map("Breaking Bad", 1, 2, 4)

        result = get_disc_start_episode("Breaking Bad", 1, 3)
        assert result == 9

    def test_get_disc_start_episode_returns_none_on_missing_data(self, tmp_path, monkeypatch):
        """Returns None when disc 1 data isn't in the cache yet."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(cache_file))
        _ensure_log()

        result = get_disc_start_episode("Breaking Bad", 1, 2)
        assert result is None


class TestGetDiscMetadataDisc:
    def test_clean_disc_label_disc_s01_d2(self):
        """SHOW_S01_D2 → season=1, disc=2."""
        _ensure_log()
        _, season, disc = clean_disc_label("BREAKING_BAD_S01_D2")
        assert season == 1
        assert disc == 2

    def test_disc_param_override_detected(self, tmp_path, monkeypatch):
        """Explicit disc= param overrides whatever label says."""
        from ripper.metadata import get_disc_metadata
        monkeypatch.setattr("ripper.metadata.extract_disc_label", lambda: "SHOW_S01_D3")
        monkeypatch.setattr("ripper.metadata.tmdb_search_tv", lambda x: None)
        monkeypatch.setattr("sys.stdin", open("/dev/null"))
        monkeypatch.setitem(state.CONFIG, "metadata_cache", str(tmp_path / "cache.json"))
        _ensure_log()

        result = get_disc_metadata(media_type="tv", season=1, disc=2)
        assert result["disc"] == 2
