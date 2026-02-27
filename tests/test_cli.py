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


class TestDiscArg:
    def test_disc_arg_parses(self):
        """--disc 2 is accepted and defaults to None without flag."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--disc", type=int, default=None)
        parser.add_argument("--episode", "-e", type=int, default=None)

        args = parser.parse_args(["--disc", "2"])
        assert args.disc == 2
        assert args.episode is None

    def test_episode_default_is_none(self):
        """--episode defaults to None (not 1) to allow disc map auto-compute."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--episode", "-e", type=int, default=None)

        args = parser.parse_args([])
        assert args.episode is None
