"""HLD §18: the role metadata this skill declares matches its real tools."""

from __future__ import annotations

import asyncio

import pytest

from vmware_avi import policy_roles


def _registry() -> set[str]:
    from vmware_avi.mcp_server.server import mcp

    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.unit
def test_every_declared_name_is_a_real_tool():
    named = set(policy_roles.ESCAPES) | set(getattr(policy_roles, "AKO_TOOLS", ()))
    assert named - _registry() == set(), sorted(named - _registry())


@pytest.mark.unit
def test_metadata_is_declared_when_policy_has_roles():
    roles = pytest.importorskip("vmware_policy.roles")
    import vmware_avi.mcp_server.server  # noqa: F401  (registers the metadata on import)

    meta = roles.metadata("avi")
    assert meta is not None and meta.version == 1
    assert meta.escapes == policy_roles.ESCAPES
    assert (meta.resolver is not None) is True


#: Reviewed: these act on the default Controller (``untagged_scope="one"``).
_DEFAULT_CONTROLLER_TOOLS = frozenset(
    {
        "check_ssl_cert_expiry",
        "disable_lb_pool_member",
        "enable_lb_pool_member",
        "get_service_engine_health",
        "get_virtual_service_analytics",
        "get_virtual_service_error_logs",
        "get_virtual_service_status",
        "list_lb_pool_members",
        "list_lb_pools",
        "list_service_engines",
        "list_ssl_certs",
        "set_virtual_service_enabled",
    }
)


@pytest.mark.unit
def test_every_tool_without_a_controller_argument_is_classified():
    """Review L1: a new Kubernetes tool must not inherit 'acts on the default Controller'."""
    from vmware_avi.mcp_server.server import mcp

    untargeted = {
        t.name
        for t in asyncio.run(mcp.list_tools())
        if "controller" not in (t.inputSchema.get("properties") or {})
    }
    unclassified = untargeted - policy_roles.AKO_TOOLS - _DEFAULT_CONTROLLER_TOOLS
    assert not unclassified, (
        f"add to policy_roles.AKO_TOOLS (acts on Kubernetes) or to the reviewed "
        f"default-Controller list here: {sorted(unclassified)}"
    )
    assert not (_DEFAULT_CONTROLLER_TOOLS - untargeted), "reviewed list names a stale tool"
