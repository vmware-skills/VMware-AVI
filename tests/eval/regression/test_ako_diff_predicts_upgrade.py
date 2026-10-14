"""`diff_ako_config` must preview what `upgrade_ako_config` actually does.

From the 2026-07-19 pre-release review. The two commands were presented as
preview-then-apply but answered different questions:

    diff:    helm diff upgrade <rel> <chart> -n <ns>
    upgrade: helm upgrade      <rel> <chart> -n <ns> --reuse-values

Without ``--reuse-values`` the diff renders the chart's defaults, so every
value the operator has customised shows up as a pending change and the preview
does not describe the upgrade. Neither command pinned a chart version either,
so an unpinned OCI reference resolved to whatever the registry tagged latest —
two runs could differ with no local change, and the diff could preview a
different chart than the upgrade then applied.
"""

from __future__ import annotations

from unittest import mock

import pytest

from vmware_avi.ops import ako_config


class _Result:
    returncode = 0
    stderr = ""

    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def helm_calls(monkeypatch):
    """Record every helm invocation, stubbing release discovery."""
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["helm", "list"]:
            return _Result('[{"name":"ako-1699","chart":"ako-1.11.1"}]')
        return _Result("")

    monkeypatch.setattr(ako_config.subprocess, "run", fake_run)
    return calls


def _helm_cmd(calls, verb):
    """Fetch the `helm <verb> ...` invocation. Matched on the subcommand slot,
    not membership — `helm diff upgrade` contains both verbs."""
    for c in calls:
        if len(c) > 1 and c[1] == verb:
            return c
    raise AssertionError(f"no `helm {verb}` command in {calls}")


def test_diff_reuses_release_values(helm_calls):
    """The regression itself. Without --reuse-values the diff compares against
    chart defaults and reports customisations as pending changes."""
    ako_config.diff_ako_config()
    assert "--reuse-values" in _helm_cmd(helm_calls, "diff")


def test_diff_and_upgrade_issue_the_same_command(helm_calls):
    """Preview and apply must differ only by the diff verb and --dry-run.
    Anything else means the preview is describing a different operation."""
    ako_config.diff_ako_config(chart_version="1.11.1")
    ako_config.upgrade_ako(dry_run=True, chart_version="1.11.1", skip_prompt=True)

    diff = [a for a in _helm_cmd(helm_calls, "diff") if a != "diff"]
    upgrade = [a for a in _helm_cmd(helm_calls, "upgrade") if a != "--dry-run"]
    assert diff[:2] == ["helm", "upgrade"], "diff should wrap `helm upgrade`"
    assert diff == upgrade, f"preview and apply diverged:\n  {diff}\n  {upgrade}"


@pytest.mark.parametrize("fn,kwargs", [
    (lambda v: ako_config.diff_ako_config(chart_version=v), {}),
    (lambda v: ako_config.upgrade_ako(dry_run=True, chart_version=v, skip_prompt=True), {}),
])
def test_chart_version_is_pinned_when_given(helm_calls, fn, kwargs):
    """An operator who pins a version must get that version, not registry latest."""
    fn("1.11.1")
    cmd = next(c for c in helm_calls if c[:2] != ["helm", "list"])
    assert "--version" in cmd
    assert cmd[cmd.index("--version") + 1] == "1.11.1"


def test_no_version_flag_when_unpinned(helm_calls):
    """Empty chart_version keeps the previous behaviour — registry latest —
    rather than passing an empty --version that helm would reject."""
    ako_config.diff_ako_config()
    cmd = _helm_cmd(helm_calls, "diff")
    assert "--version" not in cmd
