"""Tests for ripper.metadata."""

import logging
import xml.etree.ElementTree as ET

from ripper import state
from ripper.metadata import clean_disc_label, tmdb_search, write_nfo


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
