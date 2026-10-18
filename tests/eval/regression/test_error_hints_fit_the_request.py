"""A request error must not send the agent to ``vmware-avi doctor``.

Same shape as VMware-Monitor 2.4.1 (real hardware, 2026-10-17): every MCP error
carried "Run 'vmware-avi doctor' to verify Controller connectivity and
credentials." -- also for a pool name that does not exist, an out-of-range
argument, an unknown controller name, or a 4xx the Controller answered with.
Connectivity is fine in all of those, doctor reports green, and the hint buries
the remedy the message already carries.

The doctor hint stays where doctor can tell the caller something: transport and
login failures, 401/403, 5xx, a body that was not JSON (a reverse proxy
answering HTML), and anything unplanned.
"""

from __future__ import annotations

import json

import pytest
from rich.console import Console

from vmware_avi.config import AppConfig, ConfigError, ControllerConfig
from vmware_avi.connection import AviApiError, _hint_for_status
from vmware_avi.mcp_server import server as srv


def _unknown_controller() -> KeyError:
    cfg = AppConfig(controllers=(ControllerConfig(name="lab", host="avi.example.test"),))
    with pytest.raises(KeyError) as info:
        cfg.get_controller("nope")
    return info.value


REQUEST_ERRORS = [
    ValueError("duration must be non-negative"),
    AviApiError("HTTP 404", status_code=404, path="pool"),
    AviApiError("HTTP 400", status_code=400, path="pool"),
    AviApiError("HTTP 409", status_code=409, path="pool"),
]

DOCTOR_ERRORS = [
    AviApiError("unreachable", path="login"),  # no status: transport failure
    AviApiError("HTTP 401", status_code=401, path="pool"),
    AviApiError("HTTP 403", status_code=403, path="pool"),
    AviApiError("HTTP 500", status_code=500, path="pool"),
    AviApiError("HTTP 503", status_code=503, path="pool"),
    ConnectionError("dropped"),
    TimeoutError("slow"),
    ConfigError("password missing"),
    json.JSONDecodeError("Expecting value", "<html>", 0),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    KeyError("results"),  # a programming slip, not an authored message
    RuntimeError("?"),
]



def _not_doctor_first(hint: str, exc: BaseException, module) -> None:
    """A request error is not sent to doctor; a plain ValueError only conditionally.

    A generic ValueError may come from parsing the server's answer, so its hint
    must not claim connectivity is fine (review 2026-10-18); it offers doctor
    only if the message names no fix.
    """
    assert hint != module._DOCTOR_HINT
    if type(exc) is ValueError:
        assert hint == module._CHECK_MESSAGE_HINT

@pytest.mark.unit
@pytest.mark.parametrize("exc", REQUEST_ERRORS, ids=repr)
def test_a_request_error_does_not_send_the_agent_to_doctor(exc) -> None:
    _not_doctor_first(srv._hint_for(exc), exc, srv)


@pytest.mark.unit
def test_an_unknown_controller_does_not_send_the_agent_to_doctor() -> None:
    assert "doctor" not in srv._hint_for(_unknown_controller())


@pytest.mark.unit
def test_an_unknown_controller_is_still_a_key_error() -> None:
    """Callers (environment_for, policy resolvers) catch KeyError."""
    assert isinstance(_unknown_controller(), KeyError)


@pytest.mark.unit
@pytest.mark.parametrize("exc", DOCTOR_ERRORS, ids=repr)
def test_connectivity_parse_and_unplanned_errors_keep_the_doctor_hint(exc) -> None:
    assert srv._hint_for(exc) == srv._DOCTOR_HINT


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 409, 422])
def test_a_4xx_teaching_message_does_not_name_doctor(status) -> None:
    assert "doctor" not in _hint_for_status(status, "pool")


@pytest.mark.unit
@pytest.mark.parametrize("status", [500, 503])
def test_a_5xx_teaching_message_still_names_doctor(status) -> None:
    assert "doctor" in _hint_for_status(status, "pool")


# -- the string-returning tools (_capture_output / _as_error) --------------


@pytest.fixture
def op(monkeypatch):
    """An ops-style callable defined in a module that owns a ``console``."""
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "console", Console(), raising=False)

    def _make(body):
        body.__module__ = __name__
        return body

    return _make


@pytest.mark.unit
def test_an_ops_not_found_message_naming_a_tool_gets_no_doctor_hint(op) -> None:
    """pool_mgmt's own wording: it names list_lb_pools, not 'vmware-avi'."""

    def failing():
        console.print(  # noqa: F821 — swapped in by _capture_output
            "[red]Pool 'web' not found on this Controller. Run list_lb_pools to "
            "see available pools and copy an exact name.[/red]"
        )
        raise SystemExit(1)

    out = srv._capture_output(op(failing))
    assert out.startswith("Error:")
    assert "list_lb_pools" in out
    assert "doctor" not in out


@pytest.mark.unit
@pytest.mark.parametrize("exc", REQUEST_ERRORS[1:] + [ValueError("bad name")], ids=repr)
def test_a_raised_request_error_gets_no_doctor_hint(op, exc) -> None:
    def failing():
        raise exc

    out = srv._capture_output(op(failing))
    assert out.startswith("Error:")
    assert srv._DOCTOR_HINT not in out
    if type(exc) is ValueError:
        assert out.endswith(srv._CHECK_MESSAGE_HINT)
    else:
        assert "doctor" not in out


@pytest.mark.unit
def test_a_raised_unknown_controller_gets_no_doctor_hint(op) -> None:
    """Classified by its type, not by the CLI name in its text.

    The real message says "Run 'vmware-avi config'", which the pre-fix check
    (``"vmware-avi" in body``) already read as a remedy, so raising it as-is
    passed on the old code too. Same type, text without the CLI name: the old
    code appends the doctor hint, the fix does not.
    """
    real = _unknown_controller()
    exc = type(real)("Controller 'nope' not found. Available: lab")
    assert "vmware-avi" not in str(exc)

    def failing():
        raise exc

    out = srv._capture_output(op(failing))
    assert out.startswith("Error:")
    assert "not found" in out
    assert "doctor" not in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [ConnectionError("dropped"), RuntimeError("?"),
     json.JSONDecodeError("Expecting value", "<html>", 0)],
    ids=repr,
)
def test_a_raised_connectivity_or_unplanned_error_keeps_the_doctor_hint(op, exc) -> None:
    def failing():
        raise exc

    assert srv._DOCTOR_HINT in srv._capture_output(op(failing))


@pytest.mark.unit
def test_a_silent_failure_keeps_the_doctor_hint(op) -> None:
    def failing():
        raise SystemExit(1)

    assert srv._DOCTOR_HINT in srv._capture_output(op(failing))


# -- the gated writes (dict payloads) --------------------------------------


@pytest.mark.unit
def test_a_gated_write_request_error_gets_no_doctor_hint() -> None:
    def run():
        raise AviApiError("HTTP 404", status_code=404, path="pool")

    out = srv._gated("set_lb_pool_member_state", None, run)
    assert out["error"]
    assert "doctor" not in out.get("hint", "")


@pytest.mark.unit
def test_a_gated_write_connectivity_error_keeps_the_doctor_hint() -> None:
    def run():
        raise ConnectionError("dropped")

    out = srv._gated("set_lb_pool_member_state", None, run)
    assert out["hint"] == srv._DOCTOR_HINT


# -- CLI commands in MCP-facing text ----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", [-1, "", "soon"])
def test_a_bad_duration_does_not_tell_an_mcp_caller_to_run_a_cli_command(value) -> None:
    from vmware_avi.ops.analytics import _parse_duration_seconds

    with pytest.raises(ValueError) as info:
        _parse_duration_seconds(value)
    assert "--help" not in str(info.value)
    assert "1h" in str(info.value)
