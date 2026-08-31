"""CLI entry point for vmware-avi.

Typer-based CLI with subcommands for VS, Pool, SSL, SE, Analytics, and AKO operations.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from vmware_policy import guarded

from vmware_avi._errors import cli_errors, teach_and_exit


def _harden_console_encoding() -> None:
    """Never let one unrepresentable glyph kill a command.

    On a console whose encoding cannot carry the characters we print -- cp936 on
    the Chinese Windows boxes this family is tested on, or any ASCII locale --
    ``print`` raises ``UnicodeEncodeError`` and the whole command dies with a
    traceback. ``--help`` died that way in four repos. A mangled dash is a
    cosmetic loss; a dead ``--help`` is an outage, so the error handler is
    relaxed rather than the vocabulary narrowed.

    Best effort: ``reconfigure`` is absent when stdout has been replaced by a
    plain object (pytest capture, some MCP hosts), and losing the hardening
    there is not worth an exception at import.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


_harden_console_encoding()

app = typer.Typer(
    name="vmware-avi",
    help="AVI (NSX ALB) management and AKO Kubernetes operations.",
    no_args_is_help=True,
)

console = Console()


def _audit_write(operation: str, resource: str, parameters: dict, result: str = "success") -> None:
    """Record a CLI write operation to ~/.vmware-avi/audit.log.

    Audit failure must never block the operation — any exception is
    downgraded to a stderr warning.
    """
    try:
        from vmware_avi.notify.audit import log_operation

        log_operation(operation, resource, parameters, result=result)
    except Exception as exc:  # noqa: BLE001 — audit must never block
        print(f"WARNING: audit log write failed: {exc}", file=sys.stderr)


def _run_audited(fn, *, operation: str, resource: str, parameters: dict, **kwargs) -> None:
    """Run a write op and audit its outcome (success / failure)."""
    try:
        fn(**kwargs)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        _audit_write(operation, resource, parameters, result="success" if code == 0 else "failure")
        raise
    except Exception as exc:
        _audit_write(operation, resource, parameters, result="failure")
        # Give write commands the same auth/TLS teaching read commands get.
        teach_and_exit(exc)  # raises typer.Exit(1) for auth/TLS; else returns
        raise
    else:
        _audit_write(operation, resource, parameters)


# --- Sub-apps ---

vs_app = typer.Typer(help="Virtual Service management", no_args_is_help=True)
pool_app = typer.Typer(help="Pool member management", no_args_is_help=True)
ssl_app = typer.Typer(help="SSL certificate management", no_args_is_help=True)
se_app = typer.Typer(help="Service Engine management", no_args_is_help=True)
ako_app = typer.Typer(help="AKO (Avi Kubernetes Operator) operations", no_args_is_help=True)

app.add_typer(vs_app, name="vs")
app.add_typer(pool_app, name="pool")
app.add_typer(ssl_app, name="ssl")
app.add_typer(se_app, name="se")
app.add_typer(ako_app, name="ako")


# --- Global commands ---


@app.command()
def doctor() -> None:
    """Run environment diagnostics."""
    from vmware_avi.doctor import run_doctor

    ok = run_doctor()
    raise SystemExit(0 if ok else 1)


@app.command("mcp")
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients:
        vmware-avi mcp

    Equivalent to the legacy `vmware-avi-mcp` console script.
    """
    import sys

    # Deliberate runtime guard: pip/uv may ignore requires-python (踩坑 #33).
    if sys.version_info < (3, 10):  # noqa: UP036
        msg = (
            f"ERROR: vmware-avi MCP server requires Python >= 3.10 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Interpreter: {sys.executable}\n"
            "Fix: uv python install 3.12 && "
            "uv tool install --python 3.12 --force vmware-avi"
        )
        typer.echo(msg, err=True)
        raise typer.Exit(2)

    from vmware_avi.mcp_server.server import main as _mcp_main

    _mcp_main()


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
    skip_test: bool = typer.Option(
        False, "--skip-test", help="Skip the post-setup connection test."
    ),
) -> None:
    """Interactive first-run setup: write config.yaml + .env, then optionally
    verify the connection with doctor."""
    from vmware_avi.init_wizard import run_init

    raise SystemExit(run_init(force=force, skip_test=skip_test))


@app.command("config")
def config_show() -> None:
    """Show current configuration (passwords masked)."""
    from vmware_avi.config import load_config

    try:
        cfg = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    console.print("\n[bold]Controllers:[/bold]")
    for c in cfg.controllers:
        console.print(f"  {c.name}: {c.host} (user={c.username}, tenant={c.tenant})")

    console.print(f"\n[bold]Default controller:[/bold] {cfg.default_controller or '(first)'}")
    console.print("\n[bold]AKO:[/bold]")
    console.print(f"  kubeconfig: {cfg.ako.kubeconfig}")
    console.print(f"  default_context: {cfg.ako.default_context or '(current)'}")
    console.print(f"  namespace: {cfg.ako.namespace}")
    console.print()


# --- VS commands ---


@vs_app.command("list")
@cli_errors
def vs_list(
    controller: str | None = typer.Option(None, help="Controller name"),
) -> None:
    """List all Virtual Services."""
    from vmware_avi.ops.vs_mgmt import list_virtual_services

    list_virtual_services(controller)


@vs_app.command("status")
def vs_status(name: str = typer.Argument(help="Virtual Service name")) -> None:
    """Show Virtual Service status details."""
    from vmware_avi.ops.vs_mgmt import show_vs_status

    show_vs_status(name)


@vs_app.command("enable")
@cli_errors
@guarded(risk_level='high')
def vs_enable(name: str = typer.Argument(help="Virtual Service name")) -> None:
    """Enable a Virtual Service."""
    from vmware_avi.ops.vs_mgmt import toggle_vs

    _run_audited(
        lambda: toggle_vs(name, enable=True),
        operation="vs_enable",
        resource=name,
        parameters={"enable": True},
    )


@vs_app.command("disable")
@cli_errors
@guarded(risk_level='high')
def vs_disable(name: str = typer.Argument(help="Virtual Service name")) -> None:
    """Disable a Virtual Service (requires confirmation)."""
    from vmware_avi.ops.vs_mgmt import toggle_vs

    _run_audited(
        lambda: toggle_vs(name, enable=False),
        operation="vs_disable",
        resource=name,
        parameters={"enable": False},
    )


# --- Pool commands ---


@pool_app.command("members")
@cli_errors
def pool_members(pool: str = typer.Argument(help="Pool name")) -> None:
    """List pool members and health status."""
    from vmware_avi.ops.pool_mgmt import list_pool_members

    list_pool_members(pool)


@pool_app.command("enable")
@cli_errors
@guarded(risk_level='high')
def pool_enable(
    pool: str = typer.Argument(help="Pool name"),
    server: str = typer.Argument(help="Server IP"),
) -> None:
    """Enable a pool member (restore traffic)."""
    from vmware_avi.ops.pool_mgmt import toggle_pool_member

    _run_audited(
        lambda: toggle_pool_member(pool, server, enable=True),
        operation="pool_member_enable",
        resource=pool,
        parameters={"server": server, "enable": True},
    )


@pool_app.command("disable")
@cli_errors
@guarded(risk_level='high')
def pool_disable(
    pool: str = typer.Argument(help="Pool name"),
    server: str = typer.Argument(help="Server IP"),
) -> None:
    """Disable a pool member (graceful drain, requires confirmation)."""
    from vmware_avi.ops.pool_mgmt import toggle_pool_member

    _run_audited(
        lambda: toggle_pool_member(pool, server, enable=False),
        operation="pool_member_disable",
        resource=pool,
        parameters={"server": server, "enable": False},
    )


# --- SSL commands ---


@ssl_app.command("list")
@cli_errors
def ssl_list_cmd() -> None:
    """List all SSL certificates."""
    from vmware_avi.ops.ssl_mgmt import list_certificates

    list_certificates()


@ssl_app.command("expiry")
def ssl_expiry(
    days: int = typer.Option(30, help="Show certs expiring within N days"),
) -> None:
    """Check SSL certificate expiry."""
    from vmware_avi.ops.ssl_mgmt import check_expiry

    check_expiry(days)


# --- SE commands ---


@se_app.command("list")
@cli_errors
def se_list_cmd() -> None:
    """List all Service Engines."""
    from vmware_avi.ops.se_mgmt import list_service_engines

    list_service_engines()


@se_app.command("health")
def se_health() -> None:
    """Check Service Engine health."""
    from vmware_avi.ops.se_mgmt import check_se_health

    check_se_health()


# --- Analytics commands ---


@app.command("analytics")
def analytics_cmd(vs_name: str = typer.Argument(help="Virtual Service name")) -> None:
    """Show VS analytics (throughput, latency, errors)."""
    from vmware_avi.ops.analytics import show_analytics

    show_analytics(vs_name)


@app.command("logs")
def logs_cmd(
    vs_name: str = typer.Argument(help="Virtual Service name"),
    since: str = typer.Option("1h", help="Time range (e.g., 1h, 30m)"),
) -> None:
    """Show VS request error logs."""
    from vmware_avi.ops.analytics import show_error_logs

    show_error_logs(vs_name, since)


# --- AKO commands ---


@ako_app.command("status")
def ako_status(
    context: str | None = typer.Option(None, help="K8s context"),
) -> None:
    """Check AKO pod status."""
    from vmware_avi.ops.ako_pod import check_ako_status

    check_ako_status(context)


@ako_app.command("logs")
def ako_logs(
    tail: int = typer.Option(100, help="Number of lines"),
    since: str = typer.Option("", help="Time range (e.g., 30m, 1h)"),
    context: str | None = typer.Option(None, help="K8s context"),
) -> None:
    """View AKO pod logs."""
    from vmware_avi.ops.ako_pod import view_ako_logs

    view_ako_logs(tail, since, context)


@ako_app.command("restart")
@cli_errors
@guarded(risk_level='high')
def ako_restart(
    context: str | None = typer.Option(None, help="K8s context"),
) -> None:
    """Restart AKO pod (requires confirmation)."""
    from vmware_avi.ops.ako_pod import restart_ako

    _run_audited(
        lambda: restart_ako(context),
        operation="ako_restart",
        resource="ako-pod",
        parameters={"context": context or ""},
    )


@ako_app.command("version")
def ako_version(
    context: str | None = typer.Option(None, help="K8s context"),
) -> None:
    """Show AKO version info."""
    from vmware_avi.ops.ako_pod import show_ako_version

    show_ako_version(context)


# --- AKO config sub-commands (nested under ako) ---


@ako_app.command("config-show")
def ako_config_show_cmd() -> None:
    """Show current AKO values.yaml."""
    from vmware_avi.ops.ako_config import show_ako_config

    show_ako_config()


@ako_app.command("config-diff")
def ako_config_diff_cmd(
    chart_version: str = typer.Option(
        "", help="Pin the chart version to compare against (default: registry latest)"
    ),
) -> None:
    """Show pending Helm changes (diff)."""
    from vmware_avi.ops.ako_config import diff_ako_config

    diff_ako_config(chart_version=chart_version)


@ako_app.command("config-upgrade")
@cli_errors
@guarded(risk_level='medium')
def ako_config_upgrade_cmd(
    dry_run: bool = typer.Option(True, help="Preview only (default: true)"),
    chart_version: str = typer.Option(
        "", help="Pin the chart version to upgrade to (default: registry latest)"
    ),
) -> None:
    """Helm upgrade AKO (requires confirmation)."""
    from vmware_avi.ops.ako_config import upgrade_ako

    if dry_run:
        upgrade_ako(dry_run, chart_version=chart_version)
        return
    _run_audited(
        lambda: upgrade_ako(dry_run, chart_version=chart_version),
        operation="ako_config_upgrade",
        resource="ako-helm-release",
        parameters={"dry_run": False, "chart_version": chart_version or "latest"},
    )


# --- AKO ingress sub-commands ---


@ako_app.command("ingress-check")
def ako_ingress_check_cmd(
    namespace: str = typer.Argument(help="Namespace to check"),
) -> None:
    """Validate Ingress annotations in a namespace."""
    from vmware_avi.ops.ako_ingress import check_ingress_annotations

    check_ingress_annotations(namespace)


@ako_app.command("ingress-map")
def ako_ingress_map_cmd() -> None:
    """Show Ingress to VS mapping."""
    from vmware_avi.ops.ako_ingress import show_ingress_map

    show_ingress_map()


@ako_app.command("ingress-diagnose")
def ako_ingress_diagnose_cmd(
    name: str = typer.Argument(help="Ingress name"),
    namespace: str = typer.Option("default", help="Namespace"),
) -> None:
    """Diagnose why an Ingress has no VS."""
    from vmware_avi.ops.ako_ingress import diagnose_ingress

    diagnose_ingress(name, namespace)


# --- AKO sync sub-commands ---


@ako_app.command("sync-status")
def ako_sync_status_cmd() -> None:
    """Check K8s-Controller sync status."""
    from vmware_avi.ops.ako_sync import check_sync_status

    check_sync_status()


@ako_app.command("sync-diff")
def ako_sync_diff_cmd() -> None:
    """Show K8s-Controller inconsistencies."""
    from vmware_avi.ops.ako_sync import show_sync_diff

    show_sync_diff()


@ako_app.command("sync-force")
@cli_errors
@guarded(risk_level='high')
def ako_sync_force_cmd() -> None:
    """Force AKO resync (requires confirmation)."""
    from vmware_avi.ops.ako_sync import force_resync

    _run_audited(
        lambda: force_resync(),
        operation="ako_sync_force",
        resource="ako-pod",
        parameters={},
    )


# --- AKO multi-cluster ---


@ako_app.command("clusters")
def ako_clusters_cmd() -> None:
    """List all clusters with AKO deployed."""
    from vmware_avi.ops.ako_multi_cluster import list_clusters

    list_clusters()


@ako_app.command("amko-status")
def ako_amko_status_cmd() -> None:
    """Show AMKO (multi-cluster GSLB) status."""
    from vmware_avi.ops.ako_multi_cluster import show_amko_status

    show_amko_status()


if __name__ == "__main__":
    app()
