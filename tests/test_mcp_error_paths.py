"""A failed AVI tool must not look like a successful one.

Every MCP tool in this skill runs a CLI ops function and hands back whatever it
printed. The ops layer reports failure the way a CLI does — print a red message,
then ``raise SystemExit(1)`` — and ``_capture_output`` caught that exception and
discarded it. The red text came back as the tool's ordinary return value.

Nothing downstream could tell the two apart. A model asking for a Virtual
Service that does not exist received the string "Virtual Service 'web-01' not
found." as a *successful* result, which is issue #31's reported failure mode:
the agent reads it as a finding and reports it to the user as fact rather than
recognising a fault and retrying with a corrected name.

Unhandled exceptions had the opposite problem — a missing config file raised
``FileNotFoundError`` straight out of the tool with its raw text, which can
carry paths and connection detail.

These tests pin both directions: failures are unmistakable, successes are
untouched, and neither leaks raw exception text.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from vmware_avi.mcp_server import server as srv


@pytest.fixture
def op(monkeypatch):
    """Build an ops-style callable in a module that owns a ``console``.

    ``_capture_output`` swaps the *defining module's* ``console`` for a capturing
    one, so a test double has to live somewhere with that attribute. Binding it
    onto this test module mirrors how the real ops modules are laid out.
    """
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "console", Console(), raising=False)

    def _make(body):
        body.__module__ = __name__
        return body

    return _make


def test_successful_output_is_returned_unchanged(op):
    def fine():
        console.print("web-01  enabled  10.0.0.1")  # noqa: F821 — swapped in by _capture_output

    assert srv._capture_output(op(fine)).strip() == "web-01  enabled  10.0.0.1"


def test_failure_is_marked_as_an_error(op):
    def failing():
        console.print("[red]Virtual Service 'web-01' not found.[/red]")  # noqa: F821
        raise SystemExit(1)

    out = srv._capture_output(op(failing))
    assert out.startswith("Error:"), f"failure came back indistinguishable from success: {out!r}"


def test_failure_preserves_the_teaching_text(op):
    """The ops message is the useful part — wrapping must not replace it."""

    def failing():
        console.print(  # noqa: F821
            "[red]Virtual Service 'web-01' not found. "
            "Run list_virtual_services to see available Virtual Services.[/red]"
        )
        raise SystemExit(1)

    out = srv._capture_output(op(failing))
    assert "web-01" in out
    assert "list_virtual_services" in out


def test_failure_names_something_to_act_on_even_when_the_op_did_not(op):
    """A bare failure still has to leave the model somewhere to go."""

    def failing():
        console.print("[red]something went wrong[/red]")  # noqa: F821
        raise SystemExit(1)

    out = srv._capture_output(op(failing))
    assert "vmware-avi" in out


def test_clean_early_exit_is_not_an_error(op):
    """``SystemExit(0)`` is an ops function returning early, not failing."""

    def early():
        console.print("nothing to do")  # noqa: F821
        raise SystemExit(0)

    out = srv._capture_output(op(early))
    assert not out.startswith("Error:")
    assert "nothing to do" in out


def test_unexpected_exception_becomes_a_teaching_error(op):
    def broken():
        raise FileNotFoundError("/Users/someone/.vmware-avi/config.yaml")

    out = srv._capture_output(op(broken))
    assert out.startswith("Error:")
    assert "vmware-avi" in out


def test_unexpected_exception_does_not_leak_raw_text(op):
    """Exception text can carry hosts, paths and response bodies."""

    def broken():
        raise RuntimeError("https://admin:hunter2@avi.internal:443/api/virtualservice")

    out = srv._capture_output(op(broken))
    assert "hunter2" not in out


@pytest.fixture
def audit_rows(monkeypatch):
    """Capture what ``@vmware_tool`` writes to the audit log for one call."""
    rows: list[dict] = []

    class _Recorder:
        def log(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: _Recorder())
    return rows


def _run_vs_list_with(monkeypatch, body) -> str:
    """Drive the real ``list_virtual_services`` tool — decorators and all — over ``body``.

    ``list_virtual_services`` imports its ops function inside the tool body, so replacing the
    attribute on the ops module is what the tool will pick up. Going through the
    registered tool rather than calling ``_capture_output`` directly is the
    point: the audited status is produced by the ``@vmware_tool`` wrapper, and a
    test that skips the wrapper cannot see it.

    ``console`` is bound on *this* module rather than on the ops module because
    ``_capture_output`` swaps the attribute of the module a function was defined
    in, and a function's global lookup resolves there too. Pointing the two at
    different modules makes the body raise ``NameError`` instead of printing —
    which is itself a failure, so the error-path tests would have passed without
    ever reaching the code they exist to check.
    """
    import sys

    from vmware_avi.ops import vs_mgmt

    monkeypatch.setattr(sys.modules[__name__], "console", Console(), raising=False)
    monkeypatch.setattr(vs_mgmt, "list_virtual_services", body, raising=True)
    return srv.list_virtual_services()


def test_a_returned_failure_is_audited_as_a_failure(monkeypatch, audit_rows):
    """A tool that catches and returns must still be recorded as having failed.

    ``@vmware_tool`` marks a call failed when an exception reaches it or when a
    dict payload carries a truthy ``error`` key. Every tool here returns a
    *string*, so a caught failure returned normally and was audited ``ok`` — for
    ``set_virtual_service_enabled`` and ``restart_ako`` that is an audit row claiming a Virtual
    Service was disabled when it was not. It also handed vmware-pilot an undo
    token for a change that never landed and told the circuit breaker the call
    succeeded, so repeated failures never tripped it.
    """

    def failing(_controller=None):
        console.print("[red]Controller unreachable.[/red]")  # noqa: F821
        raise SystemExit(1)

    out = _run_vs_list_with(monkeypatch, failing)

    assert out.startswith("Error:")
    assert "Controller unreachable." in out, "the ops body never ran under capture"
    assert audit_rows, "the tool call was never audited at all"
    assert audit_rows[0]["status"].startswith("error"), (
        f"a failed call was audited as {audit_rows[0]['status']!r}"
    )


def test_an_unexpected_exception_is_audited_as_a_failure(monkeypatch, audit_rows):
    """The other catch path in ``_capture_output`` needs the same declaration."""

    def broken(_controller=None):
        raise RuntimeError("boom")

    out = _run_vs_list_with(monkeypatch, broken)

    assert out.startswith("Error:")
    assert audit_rows and audit_rows[0]["status"].startswith("error")


def test_a_successful_call_is_still_audited_as_ok(monkeypatch, audit_rows):
    """The failure signal must not leak into calls that worked."""

    def fine(_controller=None):
        console.print("web-01  enabled  10.0.0.1")  # noqa: F821

    out = _run_vs_list_with(monkeypatch, fine)

    assert "web-01" in out
    assert not out.startswith("Error:")
    assert audit_rows and audit_rows[0]["status"].startswith("ok")


def test_console_is_restored_after_a_failure(op):
    """Leaking the capture would silently redirect every later CLI call in the
    same process into a dead buffer: after a failed capture, printing outside a
    capture must reach the module's own console again."""
    import io
    import sys

    mod = sys.modules[__name__]
    own = io.StringIO()
    mod.console = Console(file=own, force_terminal=False)

    def failing():
        raise SystemExit(1)

    srv._capture_output(op(failing))
    mod.console.print("after the capture")
    assert "after the capture" in own.getvalue()
