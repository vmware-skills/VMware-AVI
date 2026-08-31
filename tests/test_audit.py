"""Tests for audit log writing and JSON Lines format."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from vmware_avi.notify.audit import log_operation


@pytest.mark.unit
class TestAuditLogOperation:
    """audit.log_operation — writes JSON Lines to audit.log."""

    def test_creates_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", log_file):
            log_operation("vs_disable", "web-vs", parameters={"enable": False})

        assert log_file.exists()
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["operation"] == "vs_disable"
        assert entry["resource"] == "web-vs"
        assert entry["parameters"] == {"enable": False}
        assert entry["result"] == "success"

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", log_file):
            log_operation("vs_enable", "vs-1")
            log_operation("pool_disable", "pool-1", result="failure")

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["operation"] == "vs_enable"
        assert second["result"] == "failure"

    def test_entry_has_timestamp(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", log_file):
            log_operation("test_op", "res")

        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]  # ISO-8601 contains 'T'

    def test_user_field(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", log_file):
            log_operation("delete", "vm-1", user="admin@corp")

        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["user"] == "admin@corp"

    def test_default_empty_parameters(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", log_file):
            log_operation("status", "cluster")

        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["parameters"] == {}

    def test_handles_write_error_gracefully(self, tmp_path: Path) -> None:
        # Block directory creation by putting a regular FILE where the audit
        # log's parent directory would go: mkdir(parents=True) then raises an
        # OSError (NotADirectoryError) that log_operation must swallow.
        # (Patching mkdir on a PosixPath instance is impossible — Path
        # instance attributes are read-only.)
        (tmp_path / "no_dir").write_text("", encoding="utf-8")
        bad_path = tmp_path / "no_dir" / "deep" / "audit.log"
        with patch("vmware_avi.notify.audit.AUDIT_LOG", bad_path):
            log_operation("fail_write", "x")  # should not raise
