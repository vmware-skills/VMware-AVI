"""A teaching message the agent never sees is not a teaching message.

``_safe_error`` reduces unrecognised exceptions to ``"<Class>: operation
failed."`` so raw Controller text — which can carry credentials in URLs —
cannot leak. The allowlist it checks against was an enumeration, and an
enumeration drifts: ``OSError`` was missing from it, so the one exception
``config.py`` raises — the missing-password error, this family's most common
first-run failure — reached an MCP agent as ``OSError: operation failed.``

That message's entire remedy is the env var name it carries, so redacting it
left the agent with a failure it could not act on and no way to discover the
fix. The defect was invisible from the CLI, which prints the message in full,
and invisible to the error-quality eval, which reads the message at the raise
site rather than what survives the wrapper.

So the rule is the inverse of an enumeration: every exception this skill raises
on purpose passes through, and only genuinely unplanned ones are reduced.

Admitting ``OSError`` was the wrong shape for that rule. "Exceptions this skill
raises on purpose" is a property of *this skill*, and ``OSError`` is the base
class of nearly every failure the network stack raises on its own: TLS
verification, DNS resolution, and requests' own connection errors are all
subclasses, and they carry the certificate subject, the hostname, and the full
``scheme://host:port/path`` respectively. ``sanitize`` does not redact — it
strips control characters and truncates — so admitting the base class published
all of it. The narrow ``ConfigError`` says what was actually meant.
"""

from __future__ import annotations

import socket

import pytest

from vmware_avi.config import ConfigError, ControllerConfig
from vmware_avi.connection import AviApiError
from vmware_avi.mcp_server.server import _safe_error

TEACHING = "Resource at '/api/pool' not found — run list_lb_pools to get the exact name/uuid."

CONTROLLER = "avi-prod"
ENV_KEY = "AVI_PROD_PASSWORD"


def _missing_password_error(monkeypatch) -> ConfigError:
    """Return the exception ``config.py`` actually raises, not a stand-in.

    Building an ``OSError`` by hand is how the previous version of this test
    stayed green while the allowlist and the raise site drifted apart: it
    asserted against a type nothing in the skill raises. Driving the real
    property means the type and the message are pinned together, so narrowing
    the allowlist without narrowing the raise (or the reverse) fails here.
    """
    monkeypatch.delenv(ENV_KEY, raising=False)
    with pytest.raises(ConfigError) as exc_info:
        _ = ControllerConfig(name=CONTROLLER, host="10.0.0.1").password
    return exc_info.value


def test_missing_password_keeps_the_env_var_name(monkeypatch):
    """The one error config.py raises — and the whole point of it is the name."""
    out = _safe_error(_missing_password_error(monkeypatch), "list_lb_pools")
    assert ENV_KEY in out
    assert "operation failed" not in out


def test_a_plain_oserror_is_no_longer_waved_through():
    """``ConfigError`` is admitted; its base class is not.

    The distinction is the whole fix: everything below is an ``OSError`` too.
    """
    out = _safe_error(OSError("connect to avi.internal:443 failed"), "list_virtual_services")
    assert out == "OSError: operation failed."
    assert "avi.internal" not in out


def test_dns_failure_does_not_leak_the_hostname():
    """``socket.gaierror`` is an ``OSError`` whose entire text is the hostname."""
    exc = socket.gaierror(-2, "Name or service not known: avi.internal")
    out = _safe_error(exc, "list_virtual_services")
    assert out == "gaierror: operation failed."
    assert "avi.internal" not in out


def test_avi_api_error_keeps_its_message():
    """The connection layer's teaching errors are the ones agents act on."""
    assert _safe_error(AviApiError(TEACHING, status_code=404), "pool_get") == TEACHING


@pytest.mark.parametrize("exc_type", [ValueError, FileNotFoundError, KeyError, PermissionError])
def test_validation_errors_still_pass_through(exc_type):
    assert "list_lb_pools" in _safe_error(exc_type(TEACHING), "t")


def test_dropped_connection_surfaces_its_hint():
    """The CLI path catches OSError and prints the hint; the MCP path must match."""
    assert "retry" in _safe_error(ConnectionError("Connection lost — retry the operation."), "t")


def test_unplanned_exceptions_are_still_reduced():
    """The redaction this allowlist exists for has to keep working."""
    out = _safe_error(RuntimeError("https://admin:hunter2@avi.internal/api/virtualservice"), "t")
    assert out == "RuntimeError: operation failed."
    assert "hunter2" not in out


def test_message_is_still_truncated():
    """Length capping is the other half of the guard."""
    assert len(_safe_error(AviApiError("x" * 900), "t")) <= 300
