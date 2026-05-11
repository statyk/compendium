"""Unit tests for the _safe_ident SQL-identifier guard in backup/restore."""
from __future__ import annotations

import pytest

from compendium.services.backup import BackupError, _safe_ident


class TestSafeIdent:
    def test_simple_name_passes(self):
        assert _safe_ident("app_user") == "app_user"

    def test_single_char_passes(self):
        assert _safe_ident("x") == "x"

    def test_leading_underscore_passes(self):
        assert _safe_ident("_private") == "_private"

    def test_mixed_case_passes(self):
        assert _safe_ident("WorkFTS") == "WorkFTS"

    def test_injection_attempt_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident("user; DROP TABLE app_user")

    def test_empty_string_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident("")

    def test_digit_first_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident("1abc")

    def test_quoted_name_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident('"injection"')

    def test_space_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident("a b")

    def test_hyphen_rejected(self):
        with pytest.raises(BackupError, match="unsafe SQL identifier"):
            _safe_ident("my-table")
