"""The CLI, the doctor and the MCP server must open the same config file.

Swept for after the same defect was found on real hardware in the sibling Aria
skill, 2026-08-30. This skill had its own variant, and the split runs through
the middle of the MCP server rather than between the server and the CLI:

* ``load_config`` resolved ``config_path or CONFIG_FILE`` and never looked at
  ``VMWARE_AVI_CONFIG`` at all — and every tool reaches its controller through
  an ``ops`` function that calls ``load_config()`` with no argument, so no tool
  honoured the variable;
* while the server's policy environment resolver goes through
  ``mtime_cached_loader``, which does read it.

So with the variable set, the policy engine scoped its rules ("irreversible
work in production needs a second person") from one file while the operation it
was gating ran against the controller named in another. Reproduced before the
fix, on a machine with no config at the default path at all::

    load_config()  -> FileNotFoundError: ~/.vmware-avi/config.yaml
    mtime_cached() -> controllers: ['from-env']

That is the whole defect in two lines: `vmware-avi config` told the operator
they had no configuration while the agent was already talking to a controller.

``VMWARE_AVI_CONFIG`` is this skill's advertised ``primaryEnv`` in its OpenClaw
metadata, so the CLI honouring it is the documented behaviour; ignoring it was
the bug.

The precedence now lives in exactly one function, ``resolve_config_path``, that
every reader goes through — copies of a rule do not disagree loudly, they
disagree slowly, which is how this one drifted (CLAUDE.md 形态 #6).
"""

from __future__ import annotations

import inspect
import re

import pytest

from vmware_avi import config as cfg
from vmware_avi import doctor as doc

# Deliberately different controller counts *and* names. The count says which
# file was parsed; the names say which file the per-controller checks below it
# were driven from, and without them a single check reverting on its own would
# still produce a byte-identical report.
_DEFAULT_CONTROLLER = "only-in-the-default"

_ONE_CONTROLLER = f"""
controllers:
  - name: {_DEFAULT_CONTROLLER}
    host: default.invalid
    username: admin
ako:
  kubeconfig: /nonexistent/kubeconfig
"""

_THREE_CONTROLLERS = """
controllers:
  - name: from-the-env-var-a
    host: a.invalid
    username: admin
  - name: from-the-env-var-b
    host: b.invalid
    username: admin
  - name: from-the-env-var-c
    host: c.invalid
    username: admin
ako:
  kubeconfig: /nonexistent/kubeconfig
"""


def _flat(text: str) -> str:
    """The report with whitespace and Rich's dim/colour markup stripped.

    The doctor prints one line per check with the detail in parentheses;
    flattening keeps the assertions about *which file* independent of wrapping.
    """
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┃")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A default config and .env that are both entirely valid.

    The point of making the default healthy is that the only way the doctor can
    end up reporting on the variable's file is by resolving it — a red report
    for an unrelated reason would prove nothing.
    """
    default = tmp_path / "default.yaml"
    default.write_text(_ONE_CONTROLLER, encoding="utf-8")
    env_file = tmp_path / "dot.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)

    monkeypatch.setattr(cfg, "CONFIG_FILE", default)
    monkeypatch.setattr(doc, "ENV_FILE", env_file)
    monkeypatch.delenv("VMWARE_AVI_CONFIG", raising=False)
    # Rich elides long details at 80 columns, so an assertion about a tmp_path
    # would be measuring the terminal rather than the doctor.
    monkeypatch.setenv("COLUMNS", "300")
    return default


def test_the_env_var_decides_which_file_is_resolved(sandbox, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_THREE_CONTROLLERS, encoding="utf-8")
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(elsewhere))

    assert cfg.resolve_config_path() == elsewhere
    assert len(cfg.load_config().controllers) == 3, (
        "load_config ignored $VMWARE_AVI_CONFIG, so every ops function reads "
        "one file while the policy resolver reads another"
    )


def test_an_explicit_path_still_beats_the_env_var(sandbox, tmp_path, monkeypatch):
    """The control on precedence: an explicit path is the caller saying which
    file they mean, and it has to keep winning.

    A "fix" that let the variable overtake an explicit path would pass the test
    above and break every caller that passes one.
    """
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_ONE_CONTROLLER, encoding="utf-8")
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(tmp_path / "ignored.yaml"))

    assert cfg.resolve_config_path(explicit) == explicit
    assert len(cfg.load_config(explicit).controllers) == 1


def test_with_neither_it_is_the_default(sandbox):
    assert cfg.resolve_config_path() == cfg.CONFIG_FILE
    assert len(cfg.load_config().controllers) == 1


def test_the_tools_and_the_policy_resolver_open_the_same_file(
    sandbox, tmp_path, monkeypatch
):
    """The defect itself, end to end, against the server's real loader.

    A structural test alone would not have caught this: each half was
    internally tidy, they simply disagreed. So this asserts on the thing that
    was wrong — the policy engine scoping its rules from a different file than
    the one the operation runs against.
    """
    from vmware_avi.mcp_server import server

    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_THREE_CONTROLLERS, encoding="utf-8")
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(elsewhere))

    tool_controllers = [c.name for c in cfg.load_config().controllers]
    policy_controllers = [c.name for c in server._cached_config().controllers]

    assert tool_controllers == policy_controllers, (
        f"the ops layer loaded {tool_controllers} and the policy resolver "
        f"loaded {policy_controllers}: rules scoped from one file, gating an "
        f"operation that runs against another"
    )


def test_doctor_does_not_pass_while_the_tools_cannot_load_the_config(
    sandbox, tmp_path, monkeypatch, capsys
):
    """The reported failure: the doctor clears a configuration while every tool
    call raises FileNotFoundError.

    The default config here exists and parses. It is simply not the file the
    tools will open.
    """
    missing = tmp_path / "not-there.yaml"
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(missing))

    with pytest.raises(FileNotFoundError):
        cfg.load_config()

    ok = doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    assert ok is False, (
        "doctor passed against a config file that does not exist; this is the "
        "report that tells an operator their broken setup is fine"
    )
    assert str(missing) in out, (
        "the report must name the file it looked at — a verdict about an "
        "unnamed file is what made this take real hardware to find"
    )
    assert _DEFAULT_CONTROLLER not in out, (
        "a check was driven from the default config; with the variable set, "
        "nothing should be looking at that file at all"
    )


def test_doctor_reads_the_env_vars_file_not_the_default(
    sandbox, tmp_path, monkeypatch, capsys
):
    """The positive half: pointed at a real file elsewhere, the doctor reports
    on that one — its three controllers, not the default's one."""
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(_THREE_CONTROLLERS, encoding="utf-8")
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(elsewhere))

    doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    assert str(elsewhere) in out, "the report must name the file it looked at"
    for name in ("from-the-env-var-a", "from-the-env-var-b", "from-the-env-var-c"):
        assert name in out, f"no check reached controller {name!r}"
    assert _DEFAULT_CONTROLLER not in out, (
        "a check reverted to the default config on its own and reported on its "
        "controllers"
    )


def test_the_config_directory_check_follows_the_resolved_path(
    sandbox, tmp_path, monkeypatch, capsys
):
    """The directory row must be the directory of the file in force.

    It read CONFIG_DIR, so with the variable pointing somewhere else entirely
    it reported PASS for ~/.vmware-avi — a directory with no bearing on
    anything the tools were about to do.
    """
    elsewhere = tmp_path / "nested" / "elsewhere.yaml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(_THREE_CONTROLLERS, encoding="utf-8")
    monkeypatch.setenv("VMWARE_AVI_CONFIG", str(elsewhere))

    doc.run_doctor()
    out = _flat(capsys.readouterr().out)

    # Read the row, not the page. A bare `str(elsewhere.parent) in out` passes
    # on the *config.yaml* row, whose path has the directory as a prefix — so
    # this assertion was satisfied while the directory row still named
    # ~/.vmware-avi, and the mutation proving that survived the first pass of
    # this file.
    row = re.search(r"Configdirectoryexists\(([^)]*)\)", out)
    assert row, f"no config-directory row in the report: {out[:300]}"
    assert row.group(1) == str(elsewhere.parent), (
        f"the config-directory row named {row.group(1)}, a directory unrelated "
        f"to the file the tools will open"
    )


def test_load_config_and_the_doctor_cannot_disagree():
    """Structural, not behavioural: every reader goes through the one resolver,
    so a future edit cannot silently desynchronise them again.

    The assertion is that the doctor module does not name the default config
    path or directory at all — rather than listing its checks, which would go
    stale the moment another is added. Whichever check needs to know, asks.
    """
    assert "resolve_config_path" in inspect.getsource(cfg.load_config), (
        "load_config resolves the config path by itself again; that is the "
        "duplication this test exists to prevent"
    )
    source = inspect.getsource(doc)
    for name in ("CONFIG_FILE", "CONFIG_DIR"):
        assert name not in source, (
            f"a doctor check names {name} directly, so it can diagnose a file "
            f"the tools will not open"
        )
