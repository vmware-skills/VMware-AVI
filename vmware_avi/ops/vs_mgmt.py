"""Virtual Service management operations."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from vmware_avi._safety import sanitize as _sanitize
from vmware_avi.config import load_config
from vmware_avi.connection import (
    AviApiError,
    AviConnectionManager,
    api_get,
    api_get_all,
    api_put,
)

console = Console()


def list_virtual_services(controller_name: str | None = None) -> None:
    """List all Virtual Services with status."""
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect(controller_name)

    results = api_get_all(session, "virtualservice", params={"fields": "name,enabled,uuid,vip"})

    table = Table(title="Virtual Services")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("VIP")
    table.add_column("UUID", style="dim")

    for vs in results:
        name = _sanitize(vs.get("name", ""))
        enabled = "[green]Yes[/green]" if vs.get("enabled", True) else "[red]No[/red]"
        vips = vs.get("vip", [])
        vip_str = vips[0].get("ip_address", {}).get("addr", "N/A") if vips else "N/A"
        uuid = vs.get("uuid", "")[:12]
        table.add_row(name, enabled, vip_str, uuid)

    console.print(table)


def show_vs_status(name: str) -> None:
    """Show detailed VS status including VIP, pool, runtime health and traffic."""
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    vs = session.get_object_by_name("virtualservice", name)
    if not vs:
        console.print(
            f"[red]Virtual Service '{name}' not found on this Controller. Run vs_list "
            "(CLI: vmware-avi vs list) to see available Virtual Services and copy an "
            "exact name.[/red]"
        )
        raise SystemExit(1)

    uuid = vs.get("uuid", "")

    # Fetch runtime via inventory endpoint (consolidated config + runtime).
    # Fall back gracefully if endpoint is unavailable on this Controller.
    inventory: dict = {}
    try:
        inv_resp = api_get(session, f"virtualservice-inventory/{uuid}")
        inventory = inv_resp.json() if hasattr(inv_resp, "json") else inv_resp or {}
    except Exception:
        pass

    runtime = inventory.get("runtime", {}) or {}
    oper_status = runtime.get("oper_status", {}) or {}
    metrics = inventory.get("metrics", {}) or {}

    console.print(f"\n[bold]{_sanitize(vs['name'])}[/bold]")
    console.print(f"  Enabled:   {vs.get('enabled', True)}")
    console.print(f"  UUID:      {uuid or 'N/A'}")

    # VIP(s) — VS can have multiple VIPs (IPv4/IPv6)
    vips = vs.get("vip", [])
    if vips:
        vip_strs = []
        for v in vips:
            addr = v.get("ip_address", {}).get("addr")
            addr6 = v.get("ip6_address", {}).get("addr")
            for a in (addr, addr6):
                if a:
                    vip_strs.append(a)
        console.print(f"  VIP:       {', '.join(vip_strs) or 'N/A'}")

    # Pool / Pool Group
    pool_ref = vs.get("pool_ref", "")
    if pool_ref:
        pool_name = pool_ref.split("/")[-1].split("?")[0]
        console.print(f"  Pool:      {pool_name}")
    pool_group_ref = vs.get("pool_group_ref", "")
    if pool_group_ref:
        pg_name = pool_group_ref.split("/")[-1].split("?")[0]
        console.print(f"  PoolGroup: {pg_name}")

    # Health / oper status
    oper_state = oper_status.get("state") or runtime.get("oper_status_state", "UNKNOWN")
    state_color = {
        "OPER_UP": "green",
        "OPER_AWAITING_UP": "yellow",
        "OPER_DOWN": "red",
        "OPER_DISABLED": "dim",
    }.get(oper_state, "white")
    console.print(f"  Health:    [{state_color}]{oper_state}[/{state_color}]")

    reason = oper_status.get("reason")
    if reason:
        reason_str = reason if isinstance(reason, str) else ", ".join(map(str, reason))
        console.print(f"  Reason:    {reason_str}")

    # Throughput & connection metrics (from inventory when available)
    bandwidth = metrics.get("l4_client.avg_bandwidth") or metrics.get("throughput")
    if bandwidth is not None:
        console.print(f"  Throughput: {bandwidth}")
    conn_rate = metrics.get("l4_client.avg_new_established_conns")
    if conn_rate is not None:
        console.print(f"  New conn/s: {conn_rate}")
    resp_latency = metrics.get("l7_client.avg_client_txn_latency")
    if resp_latency is not None:
        console.print(f"  Latency:   {resp_latency} ms")

    # SE placement (number of SEs serving this VS)
    se_list = runtime.get("vip_summary", [])
    if se_list:
        se_count = sum(len(v.get("service_engine", []) or []) for v in se_list)
        console.print(f"  SEs:       {se_count}")

    console.print()


def toggle_vs(name: str, *, enable: bool, skip_prompt: bool = False) -> None:
    """Enable or disable a Virtual Service.

    Args:
        name: Virtual Service name.
        enable: True to enable, False to disable.
        skip_prompt: When True, bypass the interactive double-confirm prompt.
            Used by MCP callers that enforce confirmation via the ``confirm``
            parameter before reaching this function.
    """
    action = "enable" if enable else "disable"

    if not enable and not skip_prompt:
        from vmware_avi._safety import double_confirm

        if not double_confirm(f"Disable Virtual Service '{name}'"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    vs = session.get_object_by_name("virtualservice", name)
    if not vs:
        console.print(
            f"[red]Virtual Service '{name}' not found on this Controller. Run vs_list "
            "(CLI: vmware-avi vs list) to see available Virtual Services and copy an "
            "exact name.[/red]"
        )
        raise SystemExit(1)

    vs["enabled"] = enable
    # avisdk does NOT raise on 4xx/5xx — route through api_put so a failed
    # destructive write is reported instead of printing success.
    try:
        api_put(session, f"virtualservice/{vs['uuid']}", data=vs)
    except AviApiError as exc:
        console.print(
            f"[red]Failed to {action} Virtual Service '{name}'. The Virtual Service was "
            "not changed — run vs_status to confirm its current state before retrying. "
            f"Cause: {exc}[/red]"
        )
        raise SystemExit(1) from None
    console.print(f"[green]Virtual Service '{name}' {action}d.[/green]")
