"""MCP Server for VMware AVI — stdio transport.

Exposes 28 tools (22 read, 6 write) for AVI Controller + AKO K8s operations.
Entry point: vmware-avi-mcp (defined in pyproject.toml).
"""

import logging
from pathlib import Path
from io import StringIO
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (
    describe_tool_parameters,
    mtime_cached_loader,
    report_tool_failure,
    sanitize,
    set_environment_resolver,
    vmware_tool,
    skill_name,
)

from vmware_avi.config import ConfigError
from vmware_avi.connection import AviApiError
from vmware_avi import __version__

_log = logging.getLogger("vmware-avi-mcp")

mcp = FastMCP("vmware-avi")

# FastMCP takes no version argument and leaves the lowlevel server's at
# None, which makes `initialize` answer with the MCP SDK's version rather
# than ours. Set it so a client can tell which release it is talking to.
mcp._mcp_server.version = __version__


# ---------------------------------------------------------------------------
# Output capture helper
# ---------------------------------------------------------------------------


_DOCTOR_HINT = "Run 'vmware-avi doctor' to verify Controller connectivity and credentials."


def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry Controller response bodies, credentials in
    URLs, or internal paths.

    The rule is a property, not a list: every exception this skill raises on
    purpose passes through — the builtin validation errors and this skill's own
    ``AviApiError``, which already carries a teaching message — and only
    genuinely unplanned ones are reduced. The enumeration below is the
    mechanical expression of that rule, and it drifts.

    The one exception ``config.py`` raises is admitted by its own narrow type,
    ``ConfigError`` — the missing-password error, this family's most common
    first-run failure, whose entire remedy is the env var name it carries.
    Admitting its base class ``OSError`` instead, as this list briefly did,
    admitted every OS-level error along with it, and ``sanitize`` only strips
    control characters and truncates — it redacts nothing. So
    ``ssl.SSLCertVerificationError`` (certificate subject and hostname),
    ``socket.gaierror`` (the hostname that failed to resolve) and
    ``requests.exceptions.ConnectionError`` (the full scheme://host:port/path)
    all reached the agent verbatim; each is an ``OSError`` subclass. The
    narrower ``FileNotFoundError``, ``PermissionError``, ``TimeoutError`` and
    ``ConnectionError`` stay, having been allowed all along.

    Anything else is reduced to its type, because an unplanned exception's text
    was written for a developer reading a traceback, not for an agent deciding
    what to do next, and it is the one that can carry credentials.
    """
    _log.error("Tool %s failed", tool, exc_info=True)
    _passthrough = (
        ValueError,
        FileNotFoundError,
        KeyError,
        PermissionError,
        TimeoutError,
        ConnectionError,
        ConfigError,
        AviApiError,
    )
    if isinstance(exc, _passthrough):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


def _as_error(captured: str, detail: str = "") -> str:
    """Render a failed run as a payload no reader can mistake for output.

    The ops layer reports failure the way a CLI does — print, then exit — so the
    useful teaching text is already in ``captured`` and is kept verbatim. What
    was missing is any marker that the run failed at all: without the prefix the
    model receives a red "not found" message as an ordinary successful result
    and reports it to the user as a finding (issue #31's failure mode).

    ``_DOCTOR_HINT`` is appended only when the captured text names nothing to
    act on. When the ops message already says which tool to run, repeating a
    generic "run doctor" would bury the specific advice under worse advice.

    Declaring the failure to ``@vmware_tool`` happens here rather than at the two
    catch sites, because this is the one function both of them render through and
    a renderer cannot be reached on a success path. Every tool in this skill
    returns a *string*, and the decorator only notices a failure that raises or a
    dict carrying a truthy ``error`` key — so a caught failure returned normally
    was audited ``status=ok``. For ``vs_toggle`` and ``ako_restart`` that is a row
    claiming a Virtual Service was disabled when it was not; it also handed
    vmware-pilot an undo token for a change that never landed and told the
    circuit breaker the call succeeded, so repeated failures never tripped it.
    """
    body = " ".join((captured or "").split()) or detail
    if detail and detail not in body:
        body = f"{body} {detail}".strip()
    if "vmware-avi" not in body:
        body = f"{body} {_DOCTOR_HINT}".strip()
    report_tool_failure(body)
    return f"Error: {body}"


def _capture_output(func, *args, **kwargs) -> str:
    """Run a function and capture its Rich console output as plain text.

    Failures come back as an ``Error: ...`` payload rather than as the text the
    function happened to print before dying. Both catch paths render through
    ``_as_error``, which also declares the failure to ``@vmware_tool`` — see
    there for why a returned failure has to say so out loud.
    """
    import sys

    buf = StringIO()
    from rich.console import Console

    capture_console = Console(file=buf, force_terminal=False, width=120)

    mod_name = func.__module__
    mod = sys.modules.get(mod_name)
    original_console = getattr(mod, "console", None) if mod else None

    if mod and original_console is not None:
        mod.console = capture_console

    try:
        func(*args, **kwargs)
    except SystemExit as exc:
        # A CLI ops function signals failure by exiting non-zero. `SystemExit(0)`
        # is an early return — "nothing to do" — and is not a failure.
        if exc.code:
            return _as_error(buf.getvalue())
    except Exception as exc:  # noqa: BLE001 — reduced to a safe string below
        return _as_error(buf.getvalue(), _safe_error(exc, getattr(func, "__name__", "?")))
    finally:
        if mod and original_console is not None:
            mod.console = original_console

    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# Traditional mode — AVI Controller
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def vs_list(controller: Optional[str] = None) -> str:
    """[READ] List Virtual Services on the AVI Controller.

    Returns Name, Enabled, VIP and short UUID for every VS in one call; it
    cannot be paged or filtered, and carries no health score.
    Use this before drilling into one VS with vs_status.

    Args:
        controller: Controller name from config (optional, uses default).
    """
    from vmware_avi.ops.vs_mgmt import list_virtual_services

    return _capture_output(list_virtual_services, controller)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def vs_status(name: str) -> str:
    """[READ] Detailed status for one Virtual Service: VIP, pool, health,
    connections, throughput.

    Returns one detail block, not a list. Use vs_list first for the exact name —
    a name that does not match exactly fails. Then vs_analytics for metrics,
    vs_error_logs for 5xx.

    Args:
        name: Exact Virtual Service name.
    """
    from vmware_avi.ops.vs_mgmt import show_vs_status

    return _capture_output(show_vs_status, name)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@vmware_tool(
    risk_level="high",
    undo=lambda params, result: (
        None
        if isinstance(result, str) and result.startswith("[preview]")
        else {
            "tool": "vs_toggle",
            "params": {
                "name": params.get("name"),
                "enable": not params.get("enable"),
                "confirmed": True,
            },
            "skill": "avi",
            "note": "Inverse of vs_toggle: toggle the Virtual Service back to its prior state.",
        }
    ),
)
def vs_toggle(name: str, enable: bool, confirmed: bool = False) -> str:
    """[WRITE] Enable or disable a Virtual Service. Disabling stops all traffic to it.

    Returns a one-line result. Use vs_status first to check current state.

    SAFETY: disabling requires confirmed=True; without it you get a preview
    only. Enabling is always safe.

    Args:
        name: Exact Virtual Service name.
        enable: true to enable, false to disable.
        confirmed: Gates the disable only. With enable=false, the default false
            returns a preview naming the VS that would stop serving traffic and
            changes nothing; true performs the disable. Ignored when enable=true
            — enabling always executes.
    """
    from vmware_avi.ops.vs_mgmt import toggle_vs

    if not enable and not confirmed:
        return (
            f"[preview] Would disable Virtual Service '{name}', stopping all traffic to this VS. "
            "Re-invoke with confirmed=True to execute."
        )
    return _capture_output(toggle_vs, name, enable=enable, skip_prompt=True)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def pool_list(vs_filter: Optional[str] = None) -> str:
    """[READ] Discover pools on the Controller.

    Returns Name, member count, Enabled and short UUID per pool. Use this before
    pool_members: pools are often named differently from the VS that use them.

    Args:
        vs_filter: Substring matching VS names (e.g. 'web') — returns only the
            pools those VS reference. Omit for all pools.
    """
    from vmware_avi.ops.pool_mgmt import list_pools

    return _capture_output(list_pools, vs_filter)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def pool_members(pool: str) -> str:
    """[READ] List the members of a pool.

    Returns Server IP, Port, Enabled and Ratio per member. Use before
    pool_member_enable or pool_member_disable; run pool_list first for the pool
    name. Reports configured state only, not live health-monitor results.

    Args:
        pool: Exact pool name as shown by pool_list — matched literally, not
            fuzzily, and an unknown name is refused rather than ignored. Pools
            are often named differently from the Virtual Services that use
            them, so do not infer it from a VS name.
    """
    from vmware_avi.ops.pool_mgmt import list_pool_members

    return _capture_output(list_pool_members, pool)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="medium")
def pool_member_enable(pool: str, server: str) -> str:
    """[WRITE] Enable a pool member so it receives traffic again.

    Returns a one-line confirmation. Use pool_members first to verify the server
    IP. The server must already belong to the pool — this adds nothing.

    Args:
        pool: Exact pool name as shown by pool_list — matched literally, not
            fuzzily, and an unknown name is refused rather than ignored. Pools
            are often named differently from the Virtual Services that use
            them, so do not infer it from a VS name.
        server: Server IP address.
    """
    from vmware_avi.ops.pool_mgmt import toggle_pool_member

    return _capture_output(toggle_pool_member, pool, server, enable=True)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="high")
def pool_member_disable(pool: str, server: str, confirmed: bool = False) -> str:
    """[WRITE] Disable a pool member with graceful drain — existing connections
    complete, no new traffic.

    Returns a one-line result. Use for maintenance or rolling deployments; run
    pool_members first for the server IP, pool_member_enable to reverse it.

    SAFETY: requires confirmed=True; without it you get a preview only.

    Args:
        pool: Exact pool name as shown by pool_list — matched literally, not
            fuzzily, and an unknown name is refused rather than ignored. Pools
            are often named differently from the Virtual Services that use
            them, so do not infer it from a VS name.
        server: Server IP address.
        confirmed: false (the default) returns a preview naming the member and
            pool, and changes nothing; true disables the member, draining it so
            existing connections finish while new traffic stops. Reverse it with
            pool_member_enable.
    """
    if not confirmed:
        return (
            f"[preview] Would disable pool member {server} in pool '{pool}' "
            "(graceful drain — existing connections complete, no new traffic). "
            "Re-invoke with confirmed=True to execute."
        )
    from vmware_avi.ops.pool_mgmt import toggle_pool_member

    return _capture_output(toggle_pool_member, pool, server, enable=False, skip_prompt=True)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ssl_list() -> str:
    """[READ] List SSL/TLS certificates stored on the AVI Controller.

    Returns Name, Subject, Expiry and Type per certificate, in one call that
    cannot be paged or filtered. Use for inventory or a certificate's exact name
    — use ssl_expiry_check instead for only the ones expiring soon.
    """
    from vmware_avi.ops.ssl_mgmt import list_certificates

    return _capture_output(list_certificates)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ssl_expiry_check(days: int = 30) -> str:
    """[READ] Check which SSL certificates expire within N days (default 30).

    Returns name, expiry date and days remaining, soonest first. Use this
    instead of ssl_list when you only want certificates near expiry. Expired
    certs are included, with negative days remaining.

    Args:
        days: Report certs expiring within this many days (default 30).
    """
    from vmware_avi.ops.ssl_mgmt import check_expiry

    return _capture_output(check_expiry, days)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def vs_analytics(vs_name: str) -> str:
    """[READ] Performance metrics for one Virtual Service over the last hour.

    Returns L4 (bandwidth, connections) and L7 (latency, % errors, responses)
    averages over a fixed window that cannot be changed (12 samples, 5 min
    apart). Empty output means no traffic, not an error. Use when vs_status
    shows degraded health; vs_error_logs gives per-request detail.

    Args:
        vs_name: Exact Virtual Service name, case-sensitive, from vs_list.
    """
    from vmware_avi.ops.analytics import show_analytics

    return _capture_output(show_analytics, vs_name)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def vs_error_logs(vs_name: str, since: str = "1h") -> str:
    """[READ] Recent request error logs for one Virtual Service.

    Returns up to 50 lines — timestamp, HTTP status, URI path, client IP — for
    status 400 and above. Use this instead of vs_analytics for per-request
    detail. An empty result may mean no errors, or capture disabled on the VS.

    Args:
        vs_name: Exact Virtual Service name, from vs_list.
        since: Window — seconds or '30m', '1h', '2d' (default '1h').
    """
    from vmware_avi.ops.analytics import show_error_logs

    return _capture_output(show_error_logs, vs_name, since)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def se_list() -> str:
    """[READ] List Service Engines (AVI data-plane VMs) on the Controller.

    Returns Name, management IP, status (e.g. OPER_UP) and SE Group per SE, in
    one call that cannot be paged or filtered. Use to inventory capacity or find
    an SE's name and IP — use se_health instead for degraded VS health.
    """
    from vmware_avi.ops.se_mgmt import list_service_engines

    return _capture_output(list_service_engines)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def se_health() -> str:
    """[READ] Health of every Service Engine — operational status and VS counts.

    Returns name, operational state and the number of VSes placed on each SE.
    Use when VS health degrades to check if the issue is at the SE level;
    se_list gives the management IP and SE Group, vs_status the affected VS.
    An SE hosting no VS reports 0, not an error.
    """
    from vmware_avi.ops.se_mgmt import check_se_health

    return _capture_output(check_se_health)


# ═══════════════════════════════════════════════════════════════════════════════
# AKO mode — Kubernetes
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_status(context: Optional[str] = None) -> str:
    """[READ] Check AKO (AVI Kubernetes Operator) pod status in Kubernetes.

    Returns pod name, phase, ready flag, restart count and namespace. First step
    for Ingress or LoadBalancer issues in Tanzu/K8s; follow with ako_logs when
    it is not Running. Looks in one context's AKO namespace only — run
    ako_clusters if not found.

    Args:
        context: K8s context name (optional, uses current context).
    """
    from vmware_avi.ops.ako_pod import check_ako_status

    return _capture_output(check_ako_status, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_logs(tail: int = 100, since: Optional[str] = None, context: Optional[str] = None) -> str:
    """[READ] AKO pod logs — Ingress creation failures, sync errors, Controller
    connectivity.

    Returns raw log text, not a table. Use when ako_status shows the pod
    unhealthy or ako_sync_diff reports a missing Ingress. Only the running
    container's logs are returned.

    Args:
        tail: Number of log lines (default 100).
        since: Narrows the window, e.g. '30m', '1h'.
        context: K8s context (optional, uses current).
    """
    from vmware_avi.ops.ako_pod import view_ako_logs

    return _capture_output(view_ako_logs, tail, since or "", context)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="high")
def ako_restart(context: Optional[str] = None, confirmed: bool = False) -> str:
    """[WRITE] Restart the AKO pod by deleting it — its StatefulSet recreates it.

    Returns a one-line result. Use when AKO is stuck or after config changes;
    brief traffic disruption is possible. Run ako_status afterwards, and
    ako_logs if the pod is not Running.

    SAFETY: requires confirmed=True; without it you get a preview only.

    Args:
        context: K8s context name (optional).
        confirmed: false (the default) returns a preview and changes nothing;
            true deletes the AKO pod. The StatefulSet recreates it, so this is a
            restart rather than a removal, but ingress programming pauses until
            the new pod is Running and brief traffic disruption is possible.
    """
    if not confirmed:
        ctx_hint = f" in context '{context}'" if context else ""
        return (
            f"[preview] Would delete the AKO pod{ctx_hint} — "
            "its StatefulSet will recreate it automatically. "
            "Brief traffic disruption is possible during restart. "
            "Re-invoke with confirmed=True to execute."
        )
    from vmware_avi.ops.ako_pod import restart_ako

    return _capture_output(restart_ako, context, skip_prompt=True)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_version(context: Optional[str] = None) -> str:
    """[READ] AKO version running in a cluster, read from the pod's image tag.

    Returns the pod name and an Image/Version pair per container. Use it to
    check compatibility with the Controller, and before ako_config_diff or
    ako_config_upgrade so both target the installed chart. The tag is the only
    source, so 'latest' reports 'latest', not a number.

    Args:
        context: K8s context name (optional).
    """
    from vmware_avi.ops.ako_pod import show_ako_version

    return _capture_output(show_ako_version, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_config_show() -> str:
    """[READ] The AKO Helm release's values — controller IP, cloud name, network
    settings, feature flags.

    Returns YAML as helm reports it. Use this first to read the live config; use
    ako_config_diff instead to see what an upgrade would change. Only values
    supplied at install time appear; chart defaults do not."""
    from vmware_avi.ops.ako_config import show_ako_config

    return _capture_output(show_ako_config)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
# helm renders avicredentials.password and the avi-secret Secret into its
# diff output. That is a free-form string, so the shared credential-key net
# cannot see it — the declaration is the only thing that keeps it out of the
# audit row (VMware-Policy 1.10.0). It does nothing about the *displayed* copy,
# which in MCP mode is this tool's result: diff_ako_config redacts that.
@vmware_tool(risk_level="low", sensitive_result=True)
def ako_config_diff(chart_version: str = "") -> str:
    """[READ] Pending Helm value changes that have not been applied yet.

    Returns helm's diff output; empty means nothing would change. Credential
    values in it read `<redacted>` — that is this skill blanking them, not the
    configured value.
    Use this before ako_config_upgrade — it runs the same command, so the
    preview is real. Note: with chart_version empty the registry's moving latest
    is resolved, so two runs can differ with no local change; read ako_version
    and pass it to both.

    Args:
        chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest.
    """
    from vmware_avi.ops.ako_config import diff_ako_config

    return _capture_output(diff_ako_config, chart_version=chart_version)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
# Same helm output as ako_config_diff — see the note there.
@vmware_tool(risk_level="medium", sensitive_result=True)
def ako_config_upgrade(
    dry_run: bool = True, confirmed: bool = False, chart_version: str = ""
) -> str:
    """[WRITE] Apply an AKO Helm upgrade (dry_run=true by default).

    Returns helm's output, with credential values blanked to `<redacted>` by this
    skill. Run ako_config_diff first to review the change. Finds
    the avi-system release automatically and upgrades the Broadcom OCI chart
    with --reuse-values.

    SAFETY: applying (dry_run=False) requires confirmed=True, else preview only.

    Args:
        dry_run: Preview without applying (default true).
        confirmed: Gates the real upgrade only. With dry_run=false, the default
            false returns a preview naming the chart version and changes nothing;
            true runs `helm upgrade --reuse-values` against the avi-system
            release, rolling the AKO pod. Ignored while dry_run=true, which never
            writes.
        chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest.
    """
    from vmware_avi.ops.ako_config import upgrade_ako

    if not dry_run and not confirmed:
        return (
            "[preview] Would helm-upgrade the AKO release in avi-system from the "
            f"official Broadcom OCI chart ({chart_version or 'registry latest'}) "
            "with --reuse-values. "
            "Re-invoke with confirmed=True to execute, or use dry_run=True to preview."
        )
    return _capture_output(
        upgrade_ako, dry_run, chart_version=chart_version, skip_prompt=True
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_ingress_check(namespace: str, context: Optional[str] = None) -> str:
    """[READ] Validate every Ingress in one namespace: IngressClass and TLS
    secret references that would stop AKO creating a Virtual Service.

    Returns name, IngressClass, issues and OK/ISSUES per Ingress. Run
    ako_ingress_map first for namespace names; use ako_ingress_diagnose instead
    for one named Ingress. Covers one namespace only, and TLS checks are skipped
    when its secrets cannot be listed.

    Args:
        namespace: K8s namespace to check.
        context: K8s context name (optional).
    """
    from vmware_avi.ops.ako_ingress import check_ingress_annotations

    return _capture_output(check_ingress_annotations, namespace, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_ingress_map(context: Optional[str] = None) -> str:
    """[READ] Inventory Kubernetes Ingresses across all namespaces.

    Returns Namespace, Ingress name, Host(s) and IngressClass per Ingress. Start
    here for namespace and Ingress names, then pass them to ako_ingress_check or
    ako_ingress_diagnose. Lists the K8s side only — use ako_sync_diff for
    Ingresses with no Controller object.

    Args:
        context: K8s context name (optional).
    """
    from vmware_avi.ops.ako_ingress import show_ingress_map

    return _capture_output(show_ingress_map, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_ingress_diagnose(
    name: str, namespace: str = "default", context: Optional[str] = None
) -> str:
    """[READ] Diagnose why one Ingress has no corresponding AVI Virtual Service.

    Validates IngressClass ('avi'/'avi-lb'), TLS secrets and backend Services.
    Returns annotations, a numbered issue list and kubectl fixes. Use
    ako_ingress_map first to find Ingresses lacking a VS. Checks configuration
    only; when it is clean, try ako_logs and ako_sync_status.

    Args:
        name: Exact Ingress resource name.
        namespace: Namespace holding the Ingress (default 'default').
        context: kubeconfig context (optional), from ako_clusters.
    """
    from vmware_avi.ops.ako_ingress import diagnose_ingress

    return _capture_output(diagnose_ingress, name, namespace, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_sync_status(context: Optional[str] = None) -> str:
    """[READ] Compare the number of K8s Ingresses with the number of AVI Virtual
    Services.

    Returns both counts and a match/mismatch verdict. Use this first as a cheap
    check, then ako_sync_diff for which objects differ. A count comparison only
    — in AKO shard mode many Ingresses share one VS, so a mismatch does not by
    itself mean trouble.

    Args:
        context: K8s context name (optional).
    """
    from vmware_avi.ops.ako_sync import check_sync_status

    return _capture_output(check_sync_status, context)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_sync_diff(context: Optional[str] = None) -> str:
    """[READ] List Ingresses with no matching Virtual Service or pool on the
    Controller.

    Returns Type, namespace/name and Status per suspect Ingress. Use when
    ako_sync_status reports a mismatch; ako_sync_force reconciles. Shard-mode
    Ingresses are matched heuristically against AKO pool names, so confirm a
    'Missing' result with pool_list before acting.

    Args:
        context: K8s context name (optional).
    """
    from vmware_avi.ops.ako_sync import show_sync_diff

    return _capture_output(show_sync_diff, context)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="high")
def ako_sync_force(context: Optional[str] = None, confirmed: bool = False) -> str:
    """[WRITE] Force AKO to resync all K8s resources with the AVI Controller.

    Returns a one-line result. Use when drift is detected; may cause brief
    traffic disruption. Run ako_sync_diff first to see what is out of sync,
    then ako_sync_status.

    SAFETY: requires confirmed=True; without it you get a preview only.

    Args:
        context: K8s context name (optional).
        confirmed: false (the default) returns a preview and changes nothing;
            true forces the resync, which restarts the AKO pod so it rebuilds
            every Virtual Service, pool and VS-VIP from the cluster's current
            resources — brief traffic disruption is possible.
    """
    if not confirmed:
        ctx_hint = f" in context '{context}'" if context else ""
        return (
            f"[preview] Would force AKO to resync all K8s resources with AVI Controller{ctx_hint} "
            "(restarts AKO pod — may cause brief traffic disruption). "
            "Re-invoke with confirmed=True to execute."
        )
    from vmware_avi.ops.ako_sync import force_resync

    return _capture_output(force_resync, context, skip_prompt=True)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_clusters() -> str:
    """[READ] List every Kubernetes context in the active kubeconfig and whether
    AKO is deployed there.

    Returns Context, AKO Status (pod phase or 'Not deployed') and Version per
    context. Requires kubectl on PATH; every context is probed, so unreachable
    clusters add latency. Start here for context names, then pass one to
    ako_status, ako_logs or ako_ingress_diagnose.
    """
    from vmware_avi.ops.ako_multi_cluster import list_clusters

    return _capture_output(list_clusters)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def ako_amko_status() -> str:
    """[READ] AMKO (AVI Multi-Cluster Kubernetes Operator) GSLB status.

    Returns raw kubectl output: the AMKO pods in avi-system, then the GSLBConfig
    YAML if one exists. Use this only for multi-cluster GSLB questions — for
    single-cluster AKO health use ako_status instead. Always reads the current
    kubectl context; see ako_clusters."""
    from vmware_avi.ops.ako_multi_cluster import show_amko_status

    return _capture_output(show_amko_status)


# ═══════════════════════════════════════════════════════════════════════════════
# Environment declaration
# ═══════════════════════════════════════════════════════════════════════════════


def _load_config(config_path: Optional[Path]) -> Any:
    """Deferred-import shim for the mtime cache — this module keeps
    vmware_avi imports inside functions, matching the gate section above."""
    from vmware_avi.config import load_config

    return load_config(config_path)


_cached_config = mtime_cached_loader(
    "VMWARE_AVI_CONFIG",
    Path.home() / ".vmware-avi" / "config.yaml",  # mirrors vmware_avi.config.CONFIG_FILE
    _load_config,
)


def _environment_for(target: Optional[str]) -> str:
    """Report the environment a controller declares, for policy scoping.

    Policy rules scope by environment ("irreversible work in production needs a
    second person"), and vmware-policy cannot read this skill's config itself.
    Registering this lookup is what lets those rules fire at all. Reloaded on
    config.yaml mtime change so an edit takes effect without restarting the
    server, and resolved through the same VMWARE_AVI_CONFIG override the
    connection layer uses so both agree on which file is in force. The config
    is cached via :func:`vmware_policy.mtime_cached_loader`, so repeated tool
    calls pay one ``os.stat`` instead of a full YAML parse.
    """
    try:
        return _cached_config().environment_for(target)
    except Exception:  # noqa: BLE001 — an unreadable config means "undeclared"
        return ""


# Keyed by skill: the registry used to be one process-global slot, and a
# bare `import` of any sibling's server module replaced whichever resolver
# was there -- measured turning a freeze-production-writes rule from DENY
# to ALLOW. Keyed, a resolver only ever answers for its own skill, so
# registering at import time is safe again.
set_environment_resolver(_environment_for, skill=skill_name(__name__))


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Entry point for vmware-avi-mcp."""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)
