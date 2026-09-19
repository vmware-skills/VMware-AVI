"""AKO Pod troubleshooting operations."""

from __future__ import annotations

from rich.console import Console

from vmware_avi._safety import print_external
from vmware_avi.config import load_config
from vmware_avi.k8s_connection import K8sConnectionManager

console = Console()


def _get_ako_pod_name(core_v1, namespace: str) -> str:
    """Find the AKO pod in the given namespace."""
    pods = core_v1.list_namespaced_pod(
        namespace,
        label_selector="app.kubernetes.io/name=ako",
    )
    if not pods.items:
        pods = core_v1.list_namespaced_pod(
            namespace,
            label_selector="app=ako",
        )
    if not pods.items:
        raise RuntimeError(
            "AKO pod not found. Run ako_clusters to see which contexts have AKO "
            "installed, verify the current kube-context, or set ako.namespace in "
            f"~/.vmware-avi/config.yaml. Searched namespace: '{namespace}'"
        )
    return pods.items[0].metadata.name


def check_ako_status(context: str | None = None) -> None:
    """Check AKO pod status and readiness."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)
    v1 = k8s.core_v1(context)
    ns = k8s.namespace

    try:
        pod_name = _get_ako_pod_name(v1, ns)
        pod = v1.read_namespaced_pod(pod_name, ns)

        phase = pod.status.phase
        restarts = 0
        ready = False
        if pod.status.container_statuses:
            cs = pod.status.container_statuses[0]
            restarts = cs.restart_count
            ready = cs.ready

        status_color = "green" if phase == "Running" and ready else "red"
        console.print("\n[bold]AKO Pod Status[/bold]")
        console.print(f"  Pod: {pod_name}")
        console.print(f"  Phase: [{status_color}]{phase}[/{status_color}]")
        console.print(f"  Ready: {ready}")
        console.print(f"  Restarts: {restarts}")
        console.print(f"  Namespace: {ns}")

        if restarts > 5:
            console.print(
                f"  [yellow]Warning: High restart count ({restarts}). "
                "Check logs with: vmware-avi ako logs[/yellow]"
            )
        console.print()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)


def view_ako_logs(tail: int = 100, since: str = "", context: str | None = None) -> None:
    """View AKO pod logs."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)
    v1 = k8s.core_v1(context)
    ns = k8s.namespace

    try:
        pod_name = _get_ako_pod_name(v1, ns)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    kwargs: dict = {"name": pod_name, "namespace": ns, "tail_lines": tail}
    if since:
        seconds = _parse_duration(since)
        if seconds:
            kwargs["since_seconds"] = seconds

    logs = v1.read_namespaced_pod_log(**kwargs)
    console.print(f"\n[bold]AKO Logs ({pod_name})[/bold]\n")
    # Pod logs are whatever a workload wrote to stdout, and this output is
    # what an MCP agent reads back. Print them as inert literal text.
    print_external(console, logs)


def uid_precondition(uid: str | None) -> dict:
    """Keyword arguments that make a pod delete apply only to the pod with ``uid``.

    The MCP gate measures one pod and passes its uid, so a replacement that
    appeared in between is refused by the API server (409) instead of deleted.
    ``None`` — the CLI — sends no precondition, as before.
    """
    if uid is None:
        return {}
    from kubernetes.client import V1DeleteOptions, V1Preconditions

    return {"body": V1DeleteOptions(preconditions=V1Preconditions(uid=uid))}


def restart_ako(
    context: str | None = None, *, skip_prompt: bool = False, uid: str | None = None
) -> None:
    """Restart AKO pod by deleting it (its StatefulSet recreates it).

    Args:
        context: K8s context name (optional, uses current context).
        skip_prompt: When True, bypass the interactive double-confirm prompt.
            Used by MCP callers that enforce confirmation via the ``confirm``
            parameter before reaching this function.
        uid: Delete only if the pod still has this uid (see ``uid_precondition``).
    """
    if not skip_prompt:
        from vmware_avi._safety import double_confirm

        if not double_confirm("Restart AKO pod"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)
    v1 = k8s.core_v1(context)
    ns = k8s.namespace

    try:
        pod_name = _get_ako_pod_name(v1, ns)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    v1.delete_namespaced_pod(pod_name, ns, **uid_precondition(uid))
    console.print(f"[green]AKO pod '{pod_name}' deleted. StatefulSet will recreate it.[/green]")


def show_ako_version(context: str | None = None) -> None:
    """Show AKO version from pod image."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)
    v1 = k8s.core_v1(context)
    ns = k8s.namespace

    try:
        pod_name = _get_ako_pod_name(v1, ns)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    pod = v1.read_namespaced_pod(pod_name, ns)

    images = [c.image for c in pod.spec.containers]
    console.print("\n[bold]AKO Version[/bold]")
    console.print(f"  Pod: {pod_name}")
    for img in images:
        version = img.split(":")[-1] if ":" in img else "latest"
        console.print(f"  Image: {img}")
        console.print(f"  Version: {version}")
    console.print()


def _parse_duration(s: str) -> int | None:
    """Parse duration string like '30m', '1h' to seconds."""
    s = s.strip().lower()
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("s"):
        return int(s[:-1])
    return None
