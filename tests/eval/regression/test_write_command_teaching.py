"""Regression test: write commands surface auth/TLS teaching too.

Code review found that read commands (@cli_errors) translated auth failures
into a teaching message, but write commands (routed through _run_audited) did
not — an auth failure on `vs enable` gave a raw traceback. The fix wires the
shared `teach_and_exit` into `_run_audited`. This test pins both that the
teaching reaches write commands AND that the success-path audit still fires
(a dropped `else:` branch during the fix was caught here).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from vmware_avi import cli
from vmware_avi.connection import AviApiError


def test_write_command_auth_failure_is_taught() -> None:
    def _boom(*args, **kwargs):
        raise AviApiError("login rejected", status_code=401)

    with patch("vmware_avi.ops.vs_mgmt.toggle_vs", _boom):
        result = CliRunner().invoke(cli.app, ["vs", "enable", "web-vs"])

    assert result.exit_code == 1
    # The teaching hint names the .env password var (not a raw traceback).
    assert "_PASSWORD" in result.output
    assert ".vmware-avi" in result.output


def test_write_command_success_still_audits(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.log"
    with (
        patch("vmware_avi.ops.vs_mgmt.toggle_vs"),
        patch("vmware_avi.notify.audit.AUDIT_LOG", audit_file),
    ):
        result = CliRunner().invoke(cli.app, ["vs", "enable", "web-vs"])

    assert result.exit_code == 0, result.output
    entry = json.loads(audit_file.read_text(encoding="utf-8").strip().splitlines()[0])
    assert entry["operation"] == "vs_enable"
    assert entry["resource"] == "web-vs"
