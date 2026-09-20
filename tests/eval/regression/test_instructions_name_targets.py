"""The MCP server must tell a client which Controllers exist, and how to pick one.

``initialize`` returns the server's ``instructions``, and for a skill whose
Controller tools resolve a target name that text is the only place the client
learns what is configured. This server shipped an empty string.

The consequence is measured, not hypothetical: in vmware-monitor on 2026-09-15 a
model with no listing called tools with no target, silently got the default —
a standalone ESXi host — and answered "the vCenter has 9 VMs" from it, then
reported that no vCenter was configured at all.

Three properties, because each has failed somewhere in this family:

* the listing names the configured Controllers, built from the config the tools
  really read (a hardcoded sentence drifts from the operator's file);
* both marker phrases are present, so a client gets the listing *and* the rule;
* a loader that raises still yields usable instructions — an absent config must
  not stop the server from starting, and must not be rendered as "no
  Controllers" either.
"""

from __future__ import annotations

import pytest

from vmware_avi.mcp_server import server as srv

LISTING_MARKER = "Configured targets:"
RULE_MARKER = "Choosing a target:"


class _Controller:
    def __init__(self, name: str, host: str, tenant: str = "admin") -> None:
        self.name = name
        self.host = host
        self.tenant = tenant


class _Config:
    def __init__(self, controllers, default_controller: str = "") -> None:
        self.controllers = tuple(controllers)
        self.default_controller = default_controller


def _fake_load(monkeypatch, config) -> None:
    """Replace the loader the instructions really call."""
    monkeypatch.setattr(srv, "load_config", lambda *a, **k: config)


@pytest.mark.unit
def test_every_configured_controller_is_named(monkeypatch):
    _fake_load(
        monkeypatch,
        _Config(
            [
                _Controller("lab-avi", "192.168.60.40"),
                _Controller("prod-avi", "avi-prod.example.local", tenant="tenant-a"),
            ],
            default_controller="prod-avi",
        ),
    )

    text = srv._target_instructions()

    assert "lab-avi" in text
    assert "192.168.60.40" in text
    assert "prod-avi" in text
    assert "avi-prod.example.local" in text
    # Tenant is part of "which Controller answers this question" for AVI.
    assert "tenant-a" in text
    # The default is the one a tool that takes no `controller` will use, so a
    # listing that does not mark it leaves the reader unable to predict the call.
    assert "prod-avi (avi-prod.example.local, tenant tenant-a, default)" in text
    assert "lab-avi (192.168.60.40, tenant admin)" in text


@pytest.mark.unit
def test_default_falls_back_to_the_first_controller_like_the_config_does(monkeypatch):
    """``AppConfig.active_controller`` uses entry one when no default is declared.

    If the listing marked nothing in that case, the reader would be told there is
    no default while every Controller tool quietly used the first entry.
    """
    _fake_load(
        monkeypatch,
        _Config([_Controller("only-avi", "10.0.0.5"), _Controller("other", "10.0.0.6")]),
    )

    text = srv._target_instructions()

    assert "only-avi (10.0.0.5, tenant admin, default)" in text
    assert "other (10.0.0.6, tenant admin)" in text


@pytest.mark.unit
def test_both_marker_phrases_are_present(monkeypatch):
    _fake_load(monkeypatch, _Config([_Controller("lab-avi", "192.168.60.40")]))

    text = srv._target_instructions()

    for marker in (LISTING_MARKER, RULE_MARKER):
        assert marker in text, f"instructions must contain {marker!r}"
    # The rule has to name the parameter the tools actually take. `target` is the
    # family's usual name and is wrong here: AVI's is `controller`.
    assert "`controller`" in text
    assert "`target`" not in text


@pytest.mark.unit
def test_a_broken_config_does_not_stop_the_server_from_starting(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("Config file not found. Run 'vmware-avi init'")

    monkeypatch.setattr(srv, "load_config", _boom)

    text = srv._target_instructions()

    assert text.strip()
    assert RULE_MARKER in text
    assert LISTING_MARKER in text
    # "could not look" must not read as "there are none".
    assert "could not be read" in text


@pytest.mark.unit
def test_an_empty_config_says_none_rather_than_trailing_off(monkeypatch):
    _fake_load(monkeypatch, _Config([]))

    text = srv._target_instructions()

    assert LISTING_MARKER in text
    assert "none" in text
    assert "Configured targets: ." not in text


@pytest.mark.unit
def test_the_server_object_carries_the_built_instructions(monkeypatch):
    """The text has to reach the client, not merely exist as a function."""
    assert srv.mcp.instructions
    assert RULE_MARKER in srv.mcp.instructions
    assert LISTING_MARKER in srv.mcp.instructions
