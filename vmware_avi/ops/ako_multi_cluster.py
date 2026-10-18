"""Multi-cluster AKO and AMKO status operations."""

from __future__ import annotations

import subprocess

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from vmware_avi._safety import print_command_failure, print_external, redact_text

console = Console()

#: One line per AKO pod: ``phase:image``. A range, not ``{.items[0]...}``: that
#: errors on an empty list, which made "no AKO pods" and "kubectl failed" look the
#: same. Raw string on purpose: kubectl wants the two characters backslash-n
#: inside the quotes; a real newline there is "unterminated quoted string"
#: (review 2026-10-18 — every context read as unreadable).
_AKO_POD_JSONPATH = (
    r'jsonpath={range .items[*]}{.status.phase}:{.spec.containers[0].image}{"\n"}{end}'
)


def list_clusters() -> None:
    """List all K8s contexts and their AKO deployment status."""
    result = subprocess.run(
        ["kubectl", "config", "get-contexts", "-o", "name"],
        capture_output=True,
        text=True, encoding="utf-8",
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
    unread: list[str] = []
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
                    _AKO_POD_JSONPATH,
                ],
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            table.add_row(escape(ctx), "[red]Unreachable[/red]", "-")
            unread.append((ctx, type(exc).__name__))
            continue
        if pod_check.returncode != 0:
            # kubectl could not read this context: unknown, not "not deployed".
            table.add_row(
                escape(ctx), f"[red]Unreadable (kubectl exit {pod_check.returncode})[/red]", "-"
            )
            unread.append((ctx, pod_check.stderr))
            continue
        first = next((ln for ln in pod_check.stdout.splitlines() if ln.strip()), "")
        if first:
            parts = first.split(":", 1)
            phase = parts[0] if parts else "Unknown"
            image = parts[1] if len(parts) > 1 else "N/A"
            version = image.split(":")[-1] if ":" in image else "latest"
            status_color = "green" if phase == "Running" else "red"
            table.add_row(escape(ctx), f"[{status_color}]{escape(phase)}[/{status_color}]",
                          escape(version))
        else:
            table.add_row(escape(ctx), "[dim]Not deployed[/dim]", "-")

    console.print(table)
    for ctx, cause in unread:
        # kubectl's own words, so RBAC, a dead API server and a bad kubeconfig differ.
        print_command_failure(
            console, f"[yellow]Context '{escape(ctx)}' could not be read. Cause:[/yellow]", cause
        )
    if unread:
        console.print(
            f"[yellow]{len(unread)} of {len(contexts)} context(s) could not be read; whether "
            "AKO runs there is unknown. Check each with: kubectl --context <ctx> get pods "
            "-n avi-system[/yellow]"
        )
    if contexts and len(unread) == len(contexts):
        # Nothing was readable: this overview has no answer to give.
        raise SystemExit(1)


def show_amko_status() -> None:
    """Show AMKO (multi-cluster GSLB) status."""
    # Official AMKO chart labels pods `app.kubernetes.io/name=amko`; fall
    # back to the legacy bare `app=amko` selector (same dual-selector
    # pattern as ako_pod._get_ako_pod_name).
    results = []
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
            text=True, encoding="utf-8",
            timeout=30,
        )
        results.append(result)
        if result.returncode == 0 and result.stdout.strip():
            break

    console.print("\n[bold]AMKO Status[/bold]\n")
    last = results[-1]
    failed = [r for r in results if r.returncode != 0]
    if last.returncode == 0 and last.stdout.strip():
        print_external(console, last.stdout, max_len=4000)
    elif failed:
        # "Not deployed" needs kubectl to have answered every query; a failed
        # query (no reachable cluster, no kubeconfig) means nothing is known.
        print_command_failure(
            console,
            "[red]Could not read AMKO pods: kubectl failed, so whether AMKO is deployed is "
            "unknown. Check the kube-context and KUBECONFIG: kubectl get pods -n "
            "avi-system. Cause:[/red]",
            failed[0].stderr,
        )
        raise SystemExit(1)
    else:
        console.print("  [dim]AMKO not deployed in avi-system namespace.[/dim]")

    gslb_check = subprocess.run(
        ["kubectl", "get", "gslbconfig", "-n", "avi-system", "-o", "yaml"],
        capture_output=True,
        text=True, encoding="utf-8",
        timeout=30,
    )
    if gslb_check.returncode != 0:
        print_command_failure(
            console,
            "[yellow]Could not read GSLBConfig (kubectl get gslbconfig -n avi-system): the "
            "AMKO CRDs may be missing, access denied, or the cluster unreachable. "
            "Cause:[/yellow]",
            gslb_check.stderr,
        )
    elif gslb_check.stdout.strip():
        console.print("\n[bold]GSLBConfig:[/bold]\n")
        # The one writer in this package that printed a command's stdout with a
        # bare console.print: no redaction, no sanitize, no markup=False. A
        # GSLBConfig carries gslbLeaderCredentials, and cluster-supplied YAML has
        # no business being parsed as Rich markup.
        print_external(console, redact_text(gslb_check.stdout), max_len=4000)
    console.print()
