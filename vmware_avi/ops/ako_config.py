"""AKO Helm configuration management."""

from __future__ import annotations

import json
import subprocess

from rich.console import Console

from vmware_avi._safety import (
    print_command_failure,
    print_external,
    redact_text,
    redact_yaml,
)

console = Console()


def _print_helm_failure(headline: str, stderr: str) -> None:
    """``print_command_failure`` against this module's console.

    Resolved at call time, because the MCP server rebinds ``console`` on the
    module to capture what a tool prints.
    """
    print_command_failure(console, headline, stderr)

# Official AKO chart location (Broadcom OCI registry). The legacy repo-alias
# form `avi/ako` was never the published install path.
AKO_OCI_CHART = "oci://projects.packages.broadcom.com/ako/helm-charts/ako"


def _find_ako_release(namespace: str) -> str:
    """Discover the AKO Helm release name in the given namespace.

    Official AKO installs use ``helm install --generate-name``, so the
    release is not reliably named 'ako'. Find it via ``helm list`` by
    matching the chart name prefix. Exits with a teaching error if no AKO
    release exists.
    """
    result = subprocess.run(
        ["helm", "list", "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        _print_helm_failure(
            "[red]helm list failed. Check that helm is installed and the current "
            f"kube-context can reach the cluster: helm list -n {namespace}. Cause:[/red]",
            result.stderr,
        )
        raise SystemExit(1)

    try:
        releases = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        releases = []

    for rel in releases:
        if str(rel.get("chart", "")).startswith("ako"):
            return str(rel.get("name", ""))

    console.print(
        f"[red]No AKO Helm release found in namespace '{namespace}'.[/red]\n"
        f"Inspect installed releases with: helm list -n {namespace}\n"
        f"Install AKO with: helm install --generate-name {AKO_OCI_CHART} "
        f"--version <ako-version> -f values.yaml -n {namespace}"
    )
    raise SystemExit(1)


def show_ako_config(namespace: str = "avi-system") -> None:
    """Show current AKO Helm values."""
    release = _find_ako_release(namespace)
    result = subprocess.run(
        ["helm", "get", "values", release, "-n", namespace, "-o", "yaml"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        _print_helm_failure(
            f"[red]Failed to get Helm values for AKO release '{release}'. Verify the "
            f"release still exists: helm list -n {namespace}. Cause:[/red]",
            result.stderr,
        )
        raise SystemExit(1)

    console.print(f"\n[bold]AKO Helm Values (release: {release})[/bold]\n")
    # `helm get values` returns the release's *user-supplied* values, so an AKO
    # installed with `--set avicredentials.password=...` carries that password
    # here — and in MCP mode this print is the tool result an agent reads.
    # Redact structurally first, then print inert.
    redacted = redact_yaml(result.stdout)
    if not redacted:
        console.print(
            "[yellow]Helm returned values this command could not parse as YAML, so "
            "they are withheld rather than printed unredacted. Run "
            f"'helm get values {release} -n {namespace} -o yaml' directly to inspect "
            "them.[/yellow]"
        )
        return
    print_external(console, redacted, max_len=4000)


def diff_ako_config(namespace: str = "avi-system", chart_version: str = "") -> None:
    """Show pending Helm changes via helm diff.

    Mirrors :func:`upgrade_ako`'s command exactly, ``--reuse-values`` included.
    Without that flag the diff renders the chart's defaults instead of the
    release's own values, so every local customisation shows as a pending
    change and the preview does not describe what the upgrade would do.

    Args:
        namespace: K8s namespace hosting the AKO release.
        chart_version: Pin the chart to compare against. Empty means whatever
            the registry currently tags latest — so the same diff can differ
            between two runs with no local change. Pass the same value to
            :func:`upgrade_ako` to make preview and apply agree.
    """
    release = _find_ako_release(namespace)
    cmd = ["helm", "diff", "upgrade", release, AKO_OCI_CHART, "-n", namespace, "--reuse-values"]
    if chart_version:
        cmd += ["--version", chart_version]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        _print_helm_failure(
            "[yellow]helm-diff plugin may not be installed. "
            "Install: helm plugin install https://github.com/databus23/helm-diff[/yellow]",
            result.stderr,
        )
        raise SystemExit(1)

    if not result.stdout.strip():
        console.print("[green]No pending changes.[/green]")
    else:
        console.print("\n[bold]Pending Changes[/bold]\n")
        # --reuse-values means the diff renders the release's own values, so
        # avicredentials.password is in it — and so is the avi-secret Secret it
        # renders, base64 or not. In MCP mode this print IS the tool result, so
        # declaring the tool sensitive_result only kept it out of the audit
        # database; it still arrived in the agent's context by this line.
        print_external(console, redact_text(result.stdout), max_len=4000)


def upgrade_ako(
    dry_run: bool = True,
    namespace: str = "avi-system",
    chart_version: str = "",
    *,
    skip_prompt: bool = False,
) -> None:
    """Helm upgrade AKO with confirmation.

    Args:
        dry_run: Preview changes without applying (default True).
        namespace: K8s namespace hosting the AKO release.
        chart_version: Pin the chart version to upgrade to. Empty means the
            registry's current latest, which can move between the diff and the
            upgrade. Pass the same value to :func:`diff_ako_config` so the
            preview and the apply target one chart.
        skip_prompt: When True, bypass the interactive double-confirm prompt.
            Used by MCP callers that enforce confirmation via the ``confirmed``
            parameter before reaching this function.
    """
    release = _find_ako_release(namespace)

    if not dry_run and not skip_prompt:
        from vmware_avi._safety import double_confirm

        if not double_confirm(f"Helm upgrade AKO release '{release}'"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    cmd = ["helm", "upgrade", release, AKO_OCI_CHART, "-n", namespace, "--reuse-values"]
    if chart_version:
        cmd += ["--version", chart_version]
    if dry_run:
        cmd.append("--dry-run")
        console.print("[bold]Dry-run mode (preview only):[/bold]\n")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        _print_helm_failure(
            f"[red]Helm upgrade failed for AKO release '{release}'. The release was not "
            "changed. Preview the change first with ako_config_diff, and pin "
            "chart_version so preview and apply target one chart. Cause:[/red]",
            result.stderr,
        )
        raise SystemExit(1)

    # Same exposure as the diff — helm echoes USER-SUPPLIED VALUES, which is where
    # avicredentials.password lives. See diff_ako_config.
    print_external(console, redact_text(result.stdout), max_len=4000)
    if dry_run:
        console.print("\n[yellow]This was a dry-run. Use --no-dry-run to apply.[/yellow]")
