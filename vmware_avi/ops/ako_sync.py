"""K8s-Controller sync diagnostics."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from vmware_avi.config import load_config
from vmware_avi.connection import AviConnectionManager, api_get_all
from vmware_avi.k8s_connection import K8sConnectionManager

console = Console()


def check_sync_status(context: str | None = None) -> None:
    """Check whether K8s Ingress objects are in sync with AVI Controller VS objects."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)

    from kubernetes.client import NetworkingV1Api

    net_v1 = NetworkingV1Api(k8s.get_client(context))
    ingresses = net_v1.list_ingress_for_all_namespaces()
    k8s_count = len(ingresses.items)

    mgr = AviConnectionManager(cfg)
    session = mgr.connect()
    # Page through the full VS collection — a hardcoded page_size=1000 silently
    # undercounts (and falsely reports "counts match") once an environment
    # exceeds one page.
    vs_list = api_get_all(session, "virtualservice")
    avi_count = len(vs_list)

    console.print("\n[bold]Sync Status[/bold]")
    console.print(f"  K8s Ingress objects: {k8s_count}")
    console.print(f"  AVI Virtual Services: {avi_count}")

    if k8s_count == avi_count:
        console.print("  [green]Counts match (basic check OK).[/green]")
    else:
        console.print(
            f"  [yellow]Count mismatch: {k8s_count} Ingresses vs {avi_count} VS. "
            "Run 'vmware-avi ako sync diff' for details.[/yellow]"
        )
    console.print()


def show_sync_diff(context: str | None = None) -> None:
    """Show specific inconsistencies between K8s and AVI Controller."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)

    from kubernetes.client import NetworkingV1Api

    net_v1 = NetworkingV1Api(k8s.get_client(context))
    ingresses = net_v1.list_ingress_for_all_namespaces()
    k8s_names = {f"{ing.metadata.namespace}/{ing.metadata.name}" for ing in ingresses.items}

    mgr = AviConnectionManager(cfg)
    session = mgr.connect()
    vs_list = api_get_all(session, "virtualservice")
    avi_names = {vs.get("name", "") for vs in vs_list}

    # In AKO's default shard mode, many Ingresses do NOT get a dedicated
    # Virtual Service — they are folded into shared parent VSes and represented
    # by pools/hostrules instead. Matching only against VS-name suffixes would
    # then falsely flag healthy sharded Ingresses as "Missing on Controller".
    # AKO names pools predictably from the cluster/namespace/host/path/ingress,
    # embedding the Ingress short name as a '-'-delimited token, so we also
    # check pool names before deciding an Ingress is truly missing.
    pool_names = {p.get("name", "") for p in api_get_all(session, "pool")}

    table = Table(title="Sync Diff")
    table.add_column("Type")
    table.add_column("Name")
    table.add_column("Status")

    for name in sorted(k8s_names):
        short = name.split("/")[-1]
        in_vs = short in avi_names or any(
            avi_name.endswith(f"-{short}") or avi_name.endswith(f"--{short}")
            for avi_name in avi_names
        )
        if not in_vs and not _matched_by_pool(short, pool_names):
            table.add_row("Ingress", name, "[red]Missing on Controller[/red]")

    console.print(table)
    # Caveat: AKO pool/VS naming depends on the AKO version and shard config
    # (hostname vs namespace sharding, custom prefixes), so the pool-name
    # heuristic below is best-effort. An Ingress reported "Missing" here may
    # still be served via a shared shard VS under a non-standard pool name —
    # confirm with 'vmware-avi pool list' / 'vmware-avi ako status' before
    # acting on a missing result.
    console.print(
        "[dim]Note: shard-mode Ingresses are matched heuristically against "
        "AKO pool names; verify any 'Missing' result before acting.[/dim]"
    )
    console.print()


def _matched_by_pool(ingress_short: str, pool_names: set[str]) -> bool:
    """Return True if an AKO pool name plausibly belongs to this Ingress.

    AKO derives pool names from the cluster/namespace/host/path/ingress and
    joins the parts with single ('-') or double ('--') dashes. We treat the
    Ingress short name as matched if it appears as a whole '-'-delimited token
    inside any pool name (so 'web' matches 'cluster--default-web-foo' but not
    'webstore'). This is intentionally conservative: a near-miss on the token
    boundary is better than silently hiding a genuinely missing Ingress.
    """
    if not ingress_short:
        return False
    for pool_name in pool_names:
        # Normalize the double-dash cluster separator to a single dash so the
        # token split is uniform, then compare against '-'-delimited tokens.
        tokens = pool_name.replace("--", "-").split("-")
        if ingress_short in tokens:
            return True
    return False


def force_resync(
    context: str | None = None, *, skip_prompt: bool = False, uid: str | None = None
) -> None:
    """Force AKO to resync by restarting the pod.

    Args:
        context: K8s context name (optional, uses current context).
        skip_prompt: When True, bypass the interactive double-confirm prompt.
            Used by MCP callers that enforce confirmation via the ``confirm``
            parameter before reaching this function.
        uid: Delete only if the pod still has this uid
            (``ako_pod.uid_precondition``).
    """
    if not skip_prompt:
        from vmware_avi._safety import double_confirm

        if not double_confirm("Force AKO resync (restarts AKO pod)"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)
    v1 = k8s.core_v1(context)
    ns = k8s.namespace

    from vmware_avi.ops.ako_pod import _get_ako_pod_name, uid_precondition

    pod_name = _get_ako_pod_name(v1, ns)
    v1.delete_namespaced_pod(pod_name, ns, **uid_precondition(uid))
    console.print(
        f"[green]AKO pod '{pod_name}' deleted to trigger full resync. "
        "StatefulSet will recreate it.[/green]"
    )
