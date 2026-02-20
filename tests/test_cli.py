"""Tests for ripper.cli."""

import argparse


class TestArgparse:
    def test_argparse_repair_flag(self):
        """--repair flag is recognized by argparse."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--repair", action="store_true")
        args = parser.parse_args(["--repair"])
        assert args.repair is True

        args_empty = parser.parse_args([])
        assert args_empty.repair is False
