"""Multi-cluster AKO and AMKO status operations."""

from __future__ import annotations

import subprocess

from rich.console import Console
from rich.table import Table

from vmware_avi._safety import print_command_failure, print_external, redact_text

console = Console()


def list_clusters() -> None:
    """List all K8s contexts and their AKO deployment status."""
    result = subprocess.run(
        ["kubectl", "config", "get-contexts", "-o", "name"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print_command_failure(
            console,
            "[red]Failed to list kube-contexts. Check that kubectl is on PATH and that "
            "KUBECONFIG (or ~/.kube/config) points at a readable file: kubectl config "
            "get-contexts. Cause:[/red]",
            result.stderr,
        )
        raise SystemExit(1)

    contexts = [c.strip() for c in result.stdout.strip().split("\n") if c.strip()]

    table = Table(title="AKO Cluster Overview")
    table.add_column("Context")
    table.add_column("AKO Status")
    table.add_column("AKO Version")

    # The fan-out (one kubectl call per context) is inherent: each context is a
    # distinct API server, so there is no single bulk query. We keep it, but
    # isolate each probe — a per-context timeout, and no exception is allowed to
    # abort the whole overview if one cluster is unreachable/slow.
    for ctx in contexts:
        try:
            pod_check = subprocess.run(
                [
                    "kubectl",
                    "--context",
                    ctx,
                    "get",
                    "pods",
                    "-n",
                    "avi-system",
                    "-l",
                    "app.kubernetes.io/name=ako",
                    "-o",
                    "jsonpath={.items[0].status.phase}:{.items[0].spec.containers[0].image}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            table.add_row(ctx, "[red]Unreachable[/red]", "-")
            continue
        if pod_check.returncode == 0 and pod_check.stdout:
            parts = pod_check.stdout.split(":", 1)
            phase = parts[0] if parts else "Unknown"
            image = parts[1] if len(parts) > 1 else "N/A"
            version = image.split(":")[-1] if ":" in image else "latest"
            status_color = "green" if phase == "Running" else "red"
            table.add_row(ctx, f"[{status_color}]{phase}[/{status_color}]", version)
        else:
            table.add_row(ctx, "[dim]Not deployed[/dim]", "-")

    console.print(table)


def show_amko_status() -> None:
    """Show AMKO (multi-cluster GSLB) status."""
    # Official AMKO chart labels pods `app.kubernetes.io/name=amko`; fall
    # back to the legacy bare `app=amko` selector (same dual-selector
    # pattern as ako_pod._get_ako_pod_name).
    result = None
    for selector in ("app.kubernetes.io/name=amko", "app=amko"):
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                "avi-system",
                "-l",
                selector,
                "-o",
                "wide",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            break

    console.print("\n[bold]AMKO Status[/bold]\n")
    if result.returncode != 0 or not result.stdout.strip():
        console.print("  [dim]AMKO not deployed in avi-system namespace.[/dim]")
    else:
        print_external(console, result.stdout, max_len=4000)

    gslb_check = subprocess.run(
        ["kubectl", "get", "gslbconfig", "-n", "avi-system", "-o", "yaml"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if gslb_check.returncode == 0 and gslb_check.stdout.strip():
        console.print("\n[bold]GSLBConfig:[/bold]\n")
        # The one writer in this package that printed a command's stdout with a
        # bare console.print: no redaction, no sanitize, no markup=False. A
        # GSLBConfig carries gslbLeaderCredentials, and cluster-supplied YAML has
        # no business being parsed as Rich markup.
        print_external(console, redact_text(gslb_check.stdout), max_len=4000)
    console.print()
