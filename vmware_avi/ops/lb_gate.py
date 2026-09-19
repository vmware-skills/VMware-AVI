"""The blast-radius gate in front of vs_toggle and pool_member_disable (HLD §7).

``vs_mgmt.toggle_vs`` and ``pool_mgmt.toggle_pool_member`` stay the one write
path, shared by the CLI and the MCP tools. This module is what the MCP tools
put in front of it: it measures what the write would touch with the reads those
modules and ``pool_mgmt.list_pools`` already make, refuses on a blocker or an
unreadable field, and only then runs the executor it was handed.

Nothing here is new API surface: ``virtualservice`` / ``pool`` by name,
``virtualservice-inventory/<uuid>``, and the ``pool`` / ``poolgroup``
collections are the reads this skill already issues.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vmware_avi._safety import sanitize
from vmware_avi.config import load_config
from vmware_avi.connection import (
    AviApiError,
    AviConnectionManager,
    api_get,
    api_get_all,
)
from vmware_avi.ops.write_gate import (
    MAX_LISTED,
    PREVIEW_HINT,
    GateRefusedError,
    applied,
    preview,
    refusal,
)

_READ_NEXT = "Check the object on the Controller (vs_status / pool_members) and retry."


def _connect() -> Any:
    """A session to the default Controller — the one the CLI executors use."""
    return AviConnectionManager(load_config()).connect()


def _ref_uuid(ref: Any) -> str:
    """The uuid at the end of an AVI ref URL (``.../pool/pool-1234#name?x``)."""
    return str(ref).rsplit("/", 1)[-1].split("#")[0].split("?")[0]


def _read(fn: Callable[[], Any]) -> tuple[Any, bool]:
    """``(value, ok)`` — ok is False when the read raised."""
    try:
        return fn(), True
    except (AviApiError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, False


# ─── Virtual Service ──────────────────────────────────────────────────────────


def _vs_pool_uuids(inventory: dict) -> tuple[set[str], set[str]]:
    pools = {_ref_uuid(r) for r in inventory.get("pools") or []}
    groups = {_ref_uuid(r) for r in inventory.get("poolgroups") or []}
    return pools, groups


def _measure_pools(session: Any, uuid: str) -> tuple[list[dict] | None, str | None]:
    """Pools behind a VS, each with member counts; None when unreadable.

    ``virtualservice-inventory`` flattens every directly attached pool and pool
    group, including those of K8S-managed VSes whose own ``pool_ref`` is empty
    — which is why ``list_pools`` reads it, and why this does too.
    """
    inv, ok = _read(lambda: _json(api_get(session, f"virtualservice-inventory/{uuid}")))
    if not ok or not isinstance(inv, dict):
        return None, None
    oper = ((inv.get("runtime") or {}).get("oper_status") or {}).get("state")
    pool_uuids, group_uuids = _vs_pool_uuids(inv)
    if group_uuids:
        groups, ok = _read(lambda: api_get_all(session, "poolgroup"))
        if not ok:
            return None, oper
        by_uuid = {g.get("uuid", ""): g for g in groups}
        for g in group_uuids:
            if g not in by_uuid:
                return None, oper
            members = by_uuid[g].get("members") or []
            pool_uuids = pool_uuids | {
                _ref_uuid(m["pool_ref"]) for m in members if m.get("pool_ref")
            }
    if not pool_uuids:
        return [], oper
    pools, ok = _read(
        lambda: api_get_all(session, "pool", params={"fields": "name,uuid,servers,enabled"})
    )
    if not ok:
        return None, oper
    by_uuid = {p.get("uuid", ""): p for p in pools}
    if not pool_uuids <= set(by_uuid):
        return None, oper
    rows = []
    for pu in sorted(pool_uuids):
        servers = by_uuid[pu].get("servers") or []
        rows.append(
            {
                "name": sanitize(by_uuid[pu].get("name", "")),
                "uuid": pu,
                "members": len(servers),
                "enabled_members": sum(1 for s in servers if s.get("enabled", True)),
            }
        )
    return rows, oper


def _json(resp: Any) -> Any:
    return resp.json() if hasattr(resp, "json") else resp


def measure_vs(session: Any, name: str, enable: bool) -> tuple[dict, dict]:
    """``(blast_radius, vs_object)`` for turning Virtual Service ``name`` on/off."""
    vs = session.get_object_by_name("virtualservice", name)
    if not vs:
        raise ValueError(
            f"Virtual Service '{name}' not found on this Controller. Run vs_list "
            "to see available Virtual Services and copy an exact name."
        )
    uuid = vs.get("uuid") or None
    pools, oper = _measure_pools(session, uuid) if uuid else (None, None)
    currently = bool(vs.get("enabled", True))
    unmeasured = [f for f, v in (("uuid", uuid), ("pools", pools)) if v is None]
    radius = {
        "virtual_service": sanitize(vs.get("name", name)),
        "uuid": uuid,
        "enabled": currently,
        "enabled_after": enable,
        "oper_status": sanitize(oper) if oper else None,
        "vips": [
            a
            for v in vs.get("vip") or []
            for a in (
                (v.get("ip_address") or {}).get("addr"),
                (v.get("ip6_address") or {}).get("addr"),
            )
            if a
        ][:MAX_LISTED],
        "pool_count": len(pools) if pools is not None else None,
        "member_count": sum(p["members"] for p in pools) if pools is not None else None,
        "enabled_member_count": sum(p["enabled_members"] for p in pools)
        if pools is not None
        else None,
        "pools": (pools or [])[:MAX_LISTED],
        "effect": (
            "Starts serving traffic on its VIPs."
            if enable
            else "Stops all traffic to this Virtual Service; existing clients are cut off."
        ),
        "blockers": [],
        "unmeasured": unmeasured,
    }
    return radius, vs


def vs_toggle(name: str, enable: bool, *, act: bool, apply: Callable[[], str]) -> dict:
    """Preview, noop, or run ``apply`` (the ``toggle_vs`` executor) on ``name``."""
    session = _connect()
    radius, _vs = measure_vs(session, name, enable)
    if radius["enabled"] == enable:
        state = "enabled" if enable else "disabled"
        return {
            "action": "noop",
            "blast_radius": radius,
            "hint": (
                f"Virtual Service '{radius['virtual_service']}' is already {state}; "
                "nothing to do."
            ),
        }
    if not act:
        return preview(radius, PREVIEW_HINT)
    reason = refusal(
        "vs_toggle", f"Virtual Service '{radius['virtual_service']}'", radius, _READ_NEXT
    )
    if reason:
        raise GateRefusedError(reason, radius)
    return applied(apply(), "enabled" if enable else "disabled", radius)


# ─── Pool member ──────────────────────────────────────────────────────────────


def measure_member(session: Any, pool: str, server: str) -> dict:
    """The blast radius of draining ``server`` from ``pool``."""
    obj = session.get_object_by_name("pool", pool)
    if not obj:
        raise ValueError(
            f"Pool '{pool}' not found on this Controller. Run pool_list to see "
            "available pools and copy an exact name."
        )
    servers = obj.get("servers")
    uuid = obj.get("uuid") or None
    servers_read = isinstance(servers, list)
    unmeasured = [f for f, ok in (("uuid", uuid), ("servers", servers_read)) if not ok]
    servers = servers if servers_read else []
    matches = [i for i, s in enumerate(servers) if (s.get("ip") or {}).get("addr") == server]
    if not matches and not unmeasured:
        raise ValueError(
            f"Server '{server}' is not a member of pool '{pool}'. Run pool_members "
            "to list the member IPs and copy an exact one."
        )
    # Unread is not zero: without the server list every count is None, not 0.
    enabled_now = sum(1 for s in servers if s.get("enabled", True)) if servers_read else None
    member = servers[matches[0]] if matches else {}
    member_enabled = bool(member.get("enabled", True)) if matches else None
    blockers = []
    if len(matches) > 1:
        blockers.append(
            f"{len(matches)} members of the pool share IP {server} (different ports); this tool "
            "cannot tell which one to drain. Nothing was changed — drain it from the Controller UI."
        )
    elif member_enabled and enabled_now == 1:
        blockers.append(
            "It is the pool's only enabled member: draining it leaves the pool with no server "
            "and its Virtual Services with nothing to send traffic to. Enable another member "
            "first (pool_member_enable), or take the service out on purpose with vs_toggle."
        )
    radius = {
        "pool": sanitize(obj.get("name", pool)),
        "uuid": uuid,
        "member": {
            "ip": server,
            "port": member.get("port"),
            "enabled": member_enabled,
            "ratio": member.get("ratio", 1) if matches else None,
        },
        "member_count": len(servers) if servers_read else None,
        "enabled_member_count": enabled_now,
        "enabled_member_count_after": (
            enabled_now - (1 if member_enabled else 0) if servers_read else None
        ),
        "effect": "Graceful drain: existing connections complete, no new traffic to this member.",
        "blockers": blockers,
        "unmeasured": unmeasured,
    }
    return radius


def pool_member_disable(pool: str, server: str, *, act: bool, apply: Callable[[], str]) -> dict:
    """Preview, noop, or run ``apply`` (the ``toggle_pool_member`` executor)."""
    radius = measure_member(_connect(), pool, server)
    if radius["member"]["enabled"] is False and not radius["blockers"]:
        return {
            "action": "noop",
            "blast_radius": radius,
            "hint": (
                f"Member {server} is already disabled in pool '{radius['pool']}'; nothing to do."
            ),
        }
    if not act:
        return preview(radius, PREVIEW_HINT)
    reason = refusal(
        "pool_member_disable", f"member {server} of pool '{radius['pool']}'", radius, _READ_NEXT
    )
    if reason:
        raise GateRefusedError(reason, radius)
    return applied(apply(), "drained", radius)
