"""Tests for ripper.helpers."""

import logging

from ripper import state
from ripper.helpers import hash_file, run_cmd, sanitize_filename


def _ensure_log():
    if state.log is None:
        state.log = logging.getLogger("test")


class TestSanitizeFilename:
    def test_sanitize_filename(self):
        """Strips special chars, collapses whitespace."""
        assert sanitize_filename('The Matrix: Reloaded') == 'The Matrix Reloaded'
        assert sanitize_filename('A/B\\C') == 'ABC'
        assert sanitize_filename('  Too   Many   Spaces  ') == 'Too Many Spaces'
        assert sanitize_filename('Normal Title') == 'Normal Title'
        assert sanitize_filename('Has "Quotes" <Brackets>') == 'Has Quotes Brackets'


class TestRunCmd:
    def test_run_cmd_returns_none_on_missing_binary(self):
        """FileNotFoundError → returns None."""
        _ensure_log()
        result = run_cmd(["__nonexistent_binary_12345__", "--version"])
        assert result is None

    def test_run_cmd_returns_none_on_shutdown(self):
        """Shutdown flag set → returns None."""
        _ensure_log()
        state._shutdown_requested = True
        try:
            result = run_cmd(["echo", "hello"])
            assert result is None
        finally:
            state._shutdown_requested = False


class TestHashFile:
    def test_hash_file_md5(self, tmp_path):
        """Known bytes → correct MD5 hex."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"hello world")
        h = hash_file(test_file)
        assert h == "5eb63bbbe01eeed093cb22bb8f5acdc3"
