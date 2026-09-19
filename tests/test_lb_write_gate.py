"""vs_toggle and pool_member_disable behind the HLD §7 gate (revised 2026-09-16).

* L2 — a bare call previews and sends no PUT.
* L1 — preview and acting response carry ``blast_radius``: the object's
  identity, its current state, and the pool / member counts it touches.
* L3 — ``confirm=True`` is refused when a blocker is present (the pool's last
  enabled member, an ambiguous member IP) or a field could not be read.

The legacy ``confirmed`` parameter is a deprecated alias; the conservative
reading wins when both are passed.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from vmware_avi.connection import AviApiError
from vmware_avi.mcp_server import server
from vmware_avi.ops import lb_gate, pool_mgmt, vs_mgmt

VS_UUID = "virtualservice-1111"
P1 = "pool-aaaa"
P2 = "pool-bbbb"
PG = "poolgroup-cccc"


def _vs(enabled: bool = True, uuid: str | None = VS_UUID) -> dict:
    vs = {
        "name": "web-vs",
        "enabled": enabled,
        "vip": [{"ip_address": {"addr": "10.0.0.10"}}],
        "pool_ref": "",
    }
    return {**vs, "uuid": uuid} if uuid else vs


def _servers(*states: bool, ip_base: str = "10.1.0.") -> list[dict]:
    return [
        {"ip": {"addr": f"{ip_base}{i + 1}"}, "port": 80, "enabled": s}
        for i, s in enumerate(states)
    ]


def _pool(name: str = "web-pool", uuid: str = P1, servers: Any = None) -> dict:
    return {
        "name": name,
        "uuid": uuid,
        "servers": _servers(True, True, False) if servers is None else servers,
    }


class _Controller:
    """A Controller that answers the reads lb_gate makes and records PUTs."""

    def __init__(self, *, vs=None, pool=None, inventory=None, pools=None, groups=None) -> None:
        self.vs = _vs() if vs is None else vs
        self.pool = _pool() if pool is None else pool
        self.inventory = (
            inventory
            if inventory is not None
            else {
                "runtime": {"oper_status": {"state": "OPER_UP"}},
                "pools": [f"https://ctrl/api/pool/{P1}#web-pool"],
                "poolgroups": [f"https://ctrl/api/poolgroup/{PG}"],
            }
        )
        self.pools = (
            pools
            if pools is not None
            else [
                _pool(),
                _pool("blue-pool", P2, _servers(True, True, ip_base="10.2.0.")),
            ]
        )
        self.groups = (
            groups
            if groups is not None
            else [
                {"uuid": PG, "members": [{"pool_ref": f"https://ctrl/api/pool/{P2}"}]},
            ]
        )
        self.puts: list[tuple[str, dict]] = []

    def get_object_by_name(self, kind: str, name: str) -> Any:
        """A fresh copy per read, as a real Controller answers."""
        if kind == "virtualservice":
            return copy.deepcopy(self.vs) if self.vs and name == self.vs["name"] else None
        return copy.deepcopy(self.pool) if self.pool and name == self.pool["name"] else None

    def api_get(self, _session, path: str, **_kw) -> Any:
        if isinstance(self.inventory, BaseException):
            raise self.inventory
        assert path == f"virtualservice-inventory/{VS_UUID}"
        return self.inventory

    def api_get_all(self, _session, path: str, **_kw) -> Any:
        source = {"pool": self.pools, "poolgroup": self.groups}[path]
        if isinstance(source, BaseException):
            raise source
        return source

    def api_put(self, _session, path: str, data: dict, **_kw) -> None:
        self.puts.append((path, data))


@pytest.fixture
def controller(monkeypatch):
    """Serve the gate's reads and the CLI executors' read + PUT from one Controller."""

    def install(**kw) -> _Controller:
        ctrl = _Controller(**kw)

        class _Mgr:
            def __init__(self, _cfg):
                pass

            def connect(self, *_a):
                return ctrl

        monkeypatch.setattr(lb_gate, "_connect", lambda: ctrl)
        monkeypatch.setattr(lb_gate, "api_get", ctrl.api_get)
        monkeypatch.setattr(lb_gate, "api_get_all", ctrl.api_get_all)
        for executor in (vs_mgmt, pool_mgmt):
            monkeypatch.setattr(executor, "load_config", lambda: None)
            monkeypatch.setattr(executor, "AviConnectionManager", _Mgr)
            monkeypatch.setattr(executor, "api_put", ctrl.api_put)
        return ctrl

    return install


@pytest.fixture
def audit_rows(monkeypatch):
    rows: list[dict] = []

    class _Recorder:
        def log(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: _Recorder())
    return rows


def _unreadable() -> AviApiError:
    return AviApiError("HTTP 503 from the Controller", status_code=503, path="x")


# ─── vs_toggle: L2 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enable", [False, True])
def test_vs_toggle_bare_call_previews_and_sends_no_put(controller, enable):
    ctrl = controller(vs=_vs(enabled=not enable))
    out = server.vs_toggle("web-vs", enable)
    assert out["action"] == "preview"
    assert ctrl.puts == []


# ─── vs_toggle: L1 ────────────────────────────────────────────────────────────


def test_vs_toggle_preview_states_the_blast_radius(controller):
    controller()
    br = server.vs_toggle("web-vs", False)["blast_radius"]
    assert br["virtual_service"] == "web-vs"
    assert br["uuid"] == VS_UUID
    assert br["enabled"] is True and br["enabled_after"] is False
    assert br["oper_status"] == "OPER_UP"
    assert br["vips"] == ["10.0.0.10"]
    assert br["pool_count"] == 2, "the pool behind the pool group must be counted"
    assert br["member_count"] == 5
    assert br["enabled_member_count"] == 4
    assert {p["uuid"] for p in br["pools"]} == {P1, P2}
    assert br["blockers"] == [] and br["unmeasured"] == []


def test_vs_toggle_confirm_disables_once_and_reports_what(controller):
    ctrl = controller()
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert out["action"] == "disabled"
    assert out["blast_radius"]["pool_count"] == 2
    ((path, body),) = ctrl.puts
    assert path == f"virtualservice/{VS_UUID}"
    assert body["enabled"] is False
    assert body["name"] == "web-vs"


def test_vs_toggle_confirm_enables(controller):
    ctrl = controller(vs=_vs(enabled=False))
    out = server.vs_toggle("web-vs", True, confirm=True)
    assert out["action"] == "enabled"
    assert ctrl.puts[0][1]["enabled"] is True


def test_vs_toggle_already_in_target_state_is_a_noop(controller):
    ctrl = controller(vs=_vs(enabled=False))
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert out["action"] == "noop"
    assert ctrl.puts == []


# ─── vs_toggle: L3 ────────────────────────────────────────────────────────────


def test_vs_toggle_unreadable_inventory_is_unmeasured_and_refused(controller):
    ctrl = controller(inventory=_unreadable())
    assert "pools" in server.vs_toggle("web-vs", False)["blast_radius"]["unmeasured"]
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert ctrl.puts == []
    assert "could not read pools" in out["error"]
    assert out["blast_radius"]["unmeasured"] == ["pools"]


def test_vs_toggle_unreadable_pool_collection_is_refused(controller):
    ctrl = controller(pools=_unreadable())
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert ctrl.puts == []
    assert "could not read pools" in out["error"]


def test_vs_toggle_a_pool_group_that_is_not_there_is_refused(controller):
    ctrl = controller(groups=[])
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert ctrl.puts == []
    assert "pools" in out["error"]


def test_vs_toggle_a_vs_without_uuid_is_refused(controller):
    ctrl = controller(vs=_vs(uuid=None))
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert ctrl.puts == []
    assert "uuid" in out["error"]


def test_vs_toggle_unknown_name_teaches(controller):
    ctrl = controller()
    out = server.vs_toggle("nope", False, confirm=True)
    assert "vs_list" in out["error"]
    assert ctrl.puts == []


def test_vs_toggle_refusal_is_audited_as_a_failure(controller, audit_rows):
    controller(inventory=_unreadable())
    server.vs_toggle("web-vs", False, confirm=True)
    assert audit_rows and audit_rows[0]["status"].startswith("error")


def test_vs_toggle_failed_put_is_an_error_not_a_success(controller, monkeypatch):
    controller()

    def boom(*_a, **_k):
        raise AviApiError("HTTP 409", status_code=409, path="virtualservice/x")

    monkeypatch.setattr(vs_mgmt, "api_put", boom)
    out = server.vs_toggle("web-vs", False, confirm=True)
    assert "was not changed" in out["error"]


# ─── vs_toggle: legacy alias ──────────────────────────────────────────────────


def test_vs_toggle_confirmed_true_still_acts_and_says_it_is_deprecated(controller):
    ctrl = controller()
    out = server.vs_toggle("web-vs", False, confirmed=True)
    assert out["action"] == "disabled"
    assert len(ctrl.puts) == 1
    assert "confirmed is deprecated; use confirm" in out["deprecated"]


def test_vs_toggle_explicit_confirmed_false_holds_even_with_confirm(controller):
    ctrl = controller()
    out = server.vs_toggle("web-vs", False, confirm=True, confirmed=False)
    assert out["action"] == "preview"
    assert ctrl.puts == []
    assert "deprecated" in out


def test_vs_toggle_no_alias_means_no_deprecation_note(controller):
    controller()
    assert "deprecated" not in server.vs_toggle("web-vs", False)


def test_vs_toggle_undo_is_recorded_only_for_a_change(controller, monkeypatch):
    rows: list[dict] = []

    class _Store:
        def record(self, **kw):
            rows.append(kw)
            return "undo-1"

    monkeypatch.setattr("vmware_policy.undo.get_undo_store", lambda: _Store())
    controller()
    server.vs_toggle("web-vs", False)
    assert rows == [], "a preview changed nothing"
    server.vs_toggle("web-vs", False, confirm=True)
    (row,) = rows
    assert row["undo_descriptor"]["params"] == {"name": "web-vs", "enable": True, "confirm": True}


# ─── pool_member_disable ──────────────────────────────────────────────────────


def test_pool_member_disable_bare_call_previews_and_sends_no_put(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.1.0.1")
    assert out["action"] == "preview"
    assert ctrl.puts == []


def test_pool_member_disable_preview_states_the_blast_radius(controller):
    controller()
    br = server.pool_member_disable("web-pool", "10.1.0.1")["blast_radius"]
    assert br["pool"] == "web-pool" and br["uuid"] == P1
    assert br["member"] == {"ip": "10.1.0.1", "port": 80, "enabled": True, "ratio": 1}
    assert br["member_count"] == 3
    assert br["enabled_member_count"] == 2
    assert br["enabled_member_count_after"] == 1
    assert br["blockers"] == [] and br["unmeasured"] == []


def test_pool_member_disable_confirm_drains_exactly_that_member(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirm=True)
    assert out["action"] == "drained"
    ((path, body),) = ctrl.puts
    assert path == f"pool/{P1}"
    assert [s["enabled"] for s in body["servers"]] == [False, True, False]


def test_pool_member_disable_last_enabled_member_is_refused(controller):
    ctrl = controller(pool=_pool(servers=_servers(True, False)))
    preview = server.pool_member_disable("web-pool", "10.1.0.1")
    assert preview["blast_radius"]["blockers"]
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirm=True)
    assert ctrl.puts == []
    assert "only enabled member" in out["error"]


def test_pool_member_disable_an_ambiguous_ip_is_refused(controller):
    servers = [{"ip": {"addr": "10.1.0.1"}, "port": p, "enabled": True} for p in (80, 8080)]
    ctrl = controller(pool=_pool(servers=servers))
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirm=True)
    assert ctrl.puts == []
    assert "share IP 10.1.0.1" in out["error"]


def test_pool_member_disable_unreadable_servers_is_refused(controller):
    ctrl = controller(pool={"name": "web-pool", "uuid": P1, "servers": None})
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirm=True)
    assert ctrl.puts == []
    assert "could not read servers" in out["error"]


def test_pool_member_disable_unreadable_servers_preview_shows_no_counts(controller):
    """Unread is not zero: a preview must not print member_count 0 / enabled 0."""
    ctrl = controller(pool={"name": "web-pool", "uuid": P1, "servers": None})
    out = server.pool_member_disable("web-pool", "10.1.0.1")
    assert out["action"] == "preview"
    br = out["blast_radius"]
    assert br["unmeasured"] == ["servers"]
    assert br["member_count"] is None
    assert br["enabled_member_count"] is None
    assert br["enabled_member_count_after"] is None
    assert br["member"]["enabled"] is None
    assert ctrl.puts == []


def test_pool_member_disable_already_disabled_is_a_noop(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.1.0.3", confirm=True)
    assert out["action"] == "noop"
    assert ctrl.puts == []


def test_pool_member_disable_unknown_member_teaches(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.9.9.9", confirm=True)
    assert "pool_members" in out["error"]
    assert ctrl.puts == []


def test_pool_member_disable_legacy_confirmed_acts(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirmed=True)
    assert out["action"] == "drained" and len(ctrl.puts) == 1
    assert "confirmed is deprecated" in out["deprecated"]


def test_pool_member_disable_legacy_confirmed_false_holds(controller):
    ctrl = controller()
    out = server.pool_member_disable("web-pool", "10.1.0.1", confirm=True, confirmed=False)
    assert out["action"] == "preview" and ctrl.puts == []


# ─── schema ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", ["vs_toggle", "pool_member_disable"])
def test_schema_defaults_confirm_to_false_and_alias_to_null(tool_name):
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == tool_name)
    props = tool.inputSchema["properties"]
    assert props["confirm"]["default"] is False
    assert props["confirmed"]["default"] is None
    assert "Deprecated alias for confirm" in props["confirmed"]["description"]
    assert "confirm" not in tool.inputSchema.get("required", [])
