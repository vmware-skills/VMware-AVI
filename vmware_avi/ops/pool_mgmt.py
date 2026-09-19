"""Pool member management operations."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from vmware_avi._safety import sanitize
from vmware_avi.config import load_config
from vmware_avi.connection import (
    AviApiError,
    AviConnectionManager,
    api_get,
    api_get_all,
    api_put,
)

console = Console()


def list_pools(vs_filter: str | None = None) -> None:
    """Discover pools available on the Controller.

    Supports the common case where pool names differ from VS names — users
    need to list pools before calling ``pool_members <name>``. When
    ``vs_filter`` is supplied, list only pools referenced by Virtual Services
    whose name contains the substring.
    """
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    referenced: set[str] | None = None
    if vs_filter:
        # /virtualservice omits pool associations for K8S-managed and
        # policy-driven VSes (pool_ref at the top level is ""), so filtering
        # off that endpoint produced zero matches. /virtualservice-inventory
        # flattens every directly-attached pool and pool group into
        # top-level `pools[]` / `poolgroups[]` URL arrays, so it's the
        # canonical place to discover the VS→pool graph.
        vs_resp = api_get(session, "virtualservice-inventory")
        referenced = set()
        poolgroup_uuids: set[str] = set()
        for entry in (vs_resp.json() if hasattr(vs_resp, "json") else vs_resp).get("results", []):
            name = (entry.get("config") or {}).get("name", "")
            if vs_filter.lower() not in name.lower():
                continue
            # Refs may carry '#name' / '?...' fragments — strip them so the
            # filter matches actual pool names/uuids instead of nothing.
            for ref in entry.get("pools") or []:
                referenced.add(ref.rsplit("/", 1)[-1].split("#")[0].split("?")[0])
            for ref in entry.get("poolgroups") or []:
                poolgroup_uuids.add(ref.rsplit("/", 1)[-1].split("#")[0].split("?")[0])

        # Resolve pool groups to their member pools so VSes whose traffic
        # routes through a PoolGroup still surface their underlying pools.
        # Fetch the whole poolgroup collection ONCE and reverse-map by uuid in
        # memory (same pattern as se_mgmt.check_se_health) — the previous
        # per-pool-group GET was an N+1 that scaled with the VS→PG fan-out.
        if poolgroup_uuids:
            pg_by_uuid = {pg.get("uuid", ""): pg for pg in api_get_all(session, "poolgroup")}
            for pg_uuid in poolgroup_uuids:
                pg = pg_by_uuid.get(pg_uuid)
                if not pg:
                    continue
                for member in pg.get("members") or []:
                    ref = member.get("pool_ref")
                    if ref:
                        referenced.add(ref.rsplit("/", 1)[-1].split("#")[0].split("?")[0])

    # Page through the full pool collection. The vs_filter match stays
    # client-side: it selects pools referenced by VSes whose *name* contains a
    # substring, and AVI has no server-side "VS-name substring -> pools" filter,
    # so filtering must happen against the in-memory referenced set below.
    pools = api_get_all(
        session,
        "pool",
        params={"fields": "name,uuid,servers,enabled,health_monitor_refs"},
    )

    table = Table(title=f"Pools{f' (matching VS {vs_filter!r})' if vs_filter else ''}")
    table.add_column("Name")
    table.add_column("Members")
    table.add_column("Enabled")
    table.add_column("UUID", style="dim")

    shown = 0
    for pool in pools:
        name = sanitize(pool.get("name", ""))
        uuid = pool.get("uuid", "")
        # referenced filter uses either name or uuid to match
        if referenced is not None and name not in referenced and uuid not in referenced:
            continue
        members = len(pool.get("servers", []) or [])
        enabled = "[green]Yes[/green]" if pool.get("enabled", True) else "[red]No[/red]"
        table.add_row(name, str(members), enabled, uuid[:12])
        shown += 1

    console.print(table)
    console.print(f"  Showing {shown} of {len(pools)} pools")


def list_pool_members(pool_name: str) -> None:
    """List pool members with health status."""
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    pool = session.get_object_by_name("pool", pool_name)
    if not pool:
        console.print(
            f"[red]Pool '{pool_name}' not found on this Controller. Run pool_list to "
            "see available pools and copy an exact name.[/red]"
        )
        raise SystemExit(1)

    servers = pool.get("servers", [])

    table = Table(title=f"Pool: {pool_name}")
    table.add_column("Server IP")
    table.add_column("Port")
    table.add_column("Enabled")
    table.add_column("Ratio")

    for s in servers:
        ip = s.get("ip", {}).get("addr", "N/A")
        port = str(s.get("port", "default"))
        enabled = "[green]Yes[/green]" if s.get("enabled", True) else "[red]No[/red]"
        ratio = str(s.get("ratio", 1))
        table.add_row(ip, port, enabled, ratio)

    console.print(table)
    console.print(f"  Total members: {len(servers)}")


def toggle_pool_member(
    pool_name: str, server_ip: str, *, enable: bool, skip_prompt: bool = False
) -> None:
    """Enable or disable a pool member.

    Args:
        pool_name: Pool name.
        server_ip: Server IP address.
        enable: True to enable, False to disable.
        skip_prompt: When True, bypass the interactive double-confirm prompt.
            Used by MCP callers that enforce confirmation via the ``confirm``
            parameter before reaching this function.
    """
    action = "enable" if enable else "disable"

    if not enable and not skip_prompt:
        from vmware_avi._safety import double_confirm

        if not double_confirm(f"Disable pool member '{server_ip}' in '{pool_name}'"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    pool = session.get_object_by_name("pool", pool_name)
    if not pool:
        console.print(
            f"[red]Pool '{pool_name}' not found on this Controller. Run pool_list to "
            "see available pools and copy an exact name.[/red]"
        )
        raise SystemExit(1)

    found = False
    for s in pool.get("servers", []):
        if s.get("ip", {}).get("addr") == server_ip:
            s["enabled"] = enable
            found = True
            break

    if not found:
        console.print(
            f"[red]Server '{server_ip}' is not a member of pool '{pool_name}'. Run "
            f"pool_members (CLI: vmware-avi pool members '{pool_name}') to list the "
            "member IPs and copy an exact one.[/red]"
        )
        raise SystemExit(1)

    # avisdk does NOT raise on 4xx/5xx — route through api_put so a failed
    # destructive write is reported instead of printing success.
    try:
        api_put(session, f"pool/{pool['uuid']}", data=pool)
    except AviApiError as exc:
        console.print(
            f"[red]Failed to {action} pool member '{server_ip}' in '{pool_name}'. The "
            "member was not changed — run pool_members to confirm its current state "
            f"before retrying. Cause: {exc}[/red]"
        )
        raise SystemExit(1) from None
    console.print(f"[green]Pool member '{server_ip}' {action}d in '{pool_name}'.[/green]")
