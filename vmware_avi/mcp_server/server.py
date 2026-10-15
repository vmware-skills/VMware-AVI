"""MCP Server for VMware AVI — stdio transport.

Exposes 28 tools (22 read, 6 write) for AVI Controller + AKO K8s operations.
Entry point: vmware-avi-mcp (defined in pyproject.toml).
"""

import logging
import threading
from contextvars import ContextVar
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (
    describe_tool_parameters,
    report_tool_failure,
    sanitize,
    vmware_tool,
)
from vmware_policy.pool import PoolTimeoutError

from vmware_avi import __version__
from vmware_avi.config import CONFIG_FILE, ConfigError, load_config, resolve_config_path
from vmware_avi.connection import AviApiError

_log = logging.getLogger("vmware-avi-mcp")


_BASE_INSTRUCTIONS = (
    "AVI (NSX Advanced Load Balancer) load balancing and AKO Kubernetes "
    "operations. Inspect and toggle virtual services, drain pool members, check "
    "SSL certificate expiry, Service Engine health and analytics, and "
    "troubleshoot the AKO pod, its Helm config and its Ingress sync."
)

_TARGET_RULE = (
    " Choosing a target: the tool that reads across Controllers, `list_virtual_services`, takes "
    "`controller`. Choose it from what the user asked. Every other Controller tool "
    "runs against the default controller and takes no `controller`, so a "
    "question about a different one cannot be answered per call — say which "
    "controller you can reach instead of answering from the default as if it were "
    "the one asked about. If the request does not say which controller, and the "
    "configured controllers could answer differently, ask the user which one "
    "before querying. No result names the `controller` that answered, so name it "
    "in the answer yourself. The AKO tools are Kubernetes-side and not "
    "Controller-scoped: they take `context`, a kubeconfig context, instead."
)


def _config_path_or_default() -> str:
    """The config file this server tried to read, with the home directory as ``~``.

    Which file matters: with ``VMWARE_AVI_CONFIG`` set it is not ``CONFIG_FILE``,
    and naming the one this server did not read sends the operator to edit the
    wrong file. The home directory is collapsed because the absolute form carries
    the account name, and this string goes straight into a model's context.
    Never raises — a path is not worth failing startup over.
    """
    try:
        path = resolve_config_path()
    except Exception:  # noqa: BLE001
        path = Path(CONFIG_FILE)
    try:
        return f"~/{Path(path).relative_to(Path.home())}"
    except ValueError:  # outside the home directory: nothing to collapse
        return str(path)


def _controller_listing() -> str:
    """The configured Controllers, as a client should be told about them.

    Built from the config the tools themselves read, because a hardcoded
    sentence drifts from the operator's file the day they edit it. Never
    raises: an absent or broken config must not stop the server from starting —
    the tools report that error themselves, with the remedy. When it cannot be
    read, this says so rather than implying there are none, because "no
    Controllers" and "could not look" lead a model to different next steps
    (形态 #1).
    """
    try:
        cfg = load_config()
        controllers = cfg.controllers
        # Mirrors AppConfig.active_controller: an unset default_controller means
        # the first entry, which is the Controller the tools actually use.
        default_name = controllers[0].name if controllers else ""
        if cfg.default_controller:
            default_name = cfg.default_controller
    except Exception as exc:  # noqa: BLE001 — instructions are advisory, startup is not
        # The path comes from resolve_config_path, not CONFIG_FILE: with
        # VMWARE_AVI_CONFIG set they are different files, and naming the one this
        # server did not read sends the operator to edit the wrong one. Only the
        # exception's type is quoted — its text can carry a host or a certificate
        # subject, and these instructions go straight into a model's context.
        return (
            f"Configured targets: could not be read ({type(exc).__name__}) from "
            f"{_config_path_or_default()}; run `vmware-avi doctor` to see which "
            "Controllers are configured."
        )
    if not controllers:
        return (
            f"Configured targets: none — {_config_path_or_default()} declares no "
            "Controller. Run `vmware-avi init`."
        )
    listed = "; ".join(
        f"{c.name} ({c.host}, tenant {c.tenant}"
        f"{', default' if c.name == default_name else ''})"
        for c in controllers
    )
    return f"Configured targets: {listed}."


def _target_instructions() -> str:
    """Server instructions that name the configured Controllers and how to pick one.

    `initialize` returns this text, and it is the only place a client learns
    which Controllers exist. Without it the model calls tools with no
    `controller`, gets the default, and answers confidently about the wrong
    system — measured in Monitor on 2026-09-15, where the default target was a
    standalone ESXi host and "the vCenter has 9 VMs" was read from it.
    """
    return f"{_BASE_INSTRUCTIONS} {_controller_listing()}{_TARGET_RULE}"


mcp = FastMCP("vmware-avi", instructions=_target_instructions())

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
        PoolTimeoutError,  # names the target and the remedy (vmware-policy authored)
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
    was audited ``status=ok``. For ``set_virtual_service_enabled`` and ``restart_ako`` that is a row
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


#: The console the current call prints to. Per context, so calls running at the
#: same time in worker threads (HLD §16) each capture only their own output.
_capture_target: ContextVar[Any] = ContextVar("vmware_avi_capture_console", default=None)
_route_lock = threading.Lock()


class _RoutedConsole:
    """Stands in for an ops module's ``console``: prints go to the current call's capture.

    The capture used to swap the module attribute for the length of a call. With
    two calls at once that sent one caller's output into the other's result, and
    the second call's restore left the first one's buffer installed for good.
    Outside a capture (the CLI) it is the module's own console.
    """

    def __init__(self, default: Any) -> None:
        self._default = default

    def __getattr__(self, name: str) -> Any:
        return getattr(_capture_target.get() or self._default, name)


def _route_console(mod: Any) -> None:
    with _route_lock:
        current = getattr(mod, "console", None)
        if current is not None and not isinstance(current, _RoutedConsole):
            mod.console = _RoutedConsole(current)


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

    mod = sys.modules.get(func.__module__)
    if mod is not None:
        _route_console(mod)
    token = _capture_target.set(capture_console)
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
        _capture_target.reset(token)

    return buf.getvalue()


def _gated(tool: str, deprecated: Optional[str], run) -> dict:
    """Run one gated write (HLD §7) and shape every outcome as a dict.

    A refusal (``GateRefusedError``) keeps its teaching text and the blast
    radius it measured; any other failure goes through ``_safe_error``. Both
    come back as ``{"error": ...}``, which ``@vmware_tool`` audits as a
    failure. ``deprecated`` is attached whenever a legacy alias was passed.
    """
    from vmware_avi.ops.write_gate import GateRefusedError

    try:
        out = run()
    except GateRefusedError as exc:
        _log.info("Tool %s refused: %s", tool, exc)
        out = {"error": sanitize(str(exc), 1000), "blast_radius": exc.blast_radius}
    except Exception as exc:  # noqa: BLE001 — reduced to a safe string by _safe_error
        out = {"error": _safe_error(exc, tool), "hint": _DOCTOR_HINT}
    return {**out, "deprecated": deprecated} if deprecated else out


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
def list_virtual_services(controller: Optional[str] = None) -> str:
    """[READ] List Virtual Services on the AVI Controller.

    Returns Name, Enabled, VIP and short UUID for every VS in one call; it
    cannot be paged or filtered, and carries no health score.
    Use this before drilling into one VS with get_virtual_service_status.

    Args:
        controller: Controller name from config (optional, uses default).
    """
    from vmware_avi.ops.vs_mgmt import list_virtual_services as _ops_list_virtual_services

    return _capture_output(_ops_list_virtual_services, controller)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def get_virtual_service_status(name: str) -> str:
    """[READ] Detailed status for one Virtual Service: VIP, pool, health,
    connections, throughput.

    Returns one detail block, not a list. Use list_virtual_services first for the exact name —
    a name that does not match exactly fails. Then get_virtual_service_analytics for metrics,
    get_virtual_service_error_logs for 5xx.

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
        {
            "tool": "set_virtual_service_enabled",
            "params": {
                "name": params.get("name"),
                "enable": not params.get("enable"),
                "confirm": True,
            },
            "skill": "avi",
            "note": (
                "Inverse of set_virtual_service_enabled: toggle the Virtual Service "
                "back to its prior state."
            ),
        }
        if isinstance(result, dict) and result.get("action") in ("enabled", "disabled")
        else None
    ),
)
def set_virtual_service_enabled(
    name: str,
    enable: bool,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
) -> dict:
    """[WRITE] Enable or disable a Virtual Service. Disabling stops all traffic to it.

    Without confirm=True this only previews: it returns blast_radius (the VS
    name and uuid, whether it is enabled now, its VIPs and oper status, and the
    pools and member counts behind it) and changes nothing. Show that to the
    user and get their explicit decision. Do not set confirm=True on your own
    because the user asked earlier: they have not seen what it changes yet.
    A VS already in the requested state returns action "noop".

    Refused with confirm=True: a VS whose uuid or pools cannot be read (the
    change would be blind). Use get_virtual_service_status first to check current state.

    Args:
        name: Exact Virtual Service name.
        enable: true to enable, false to disable.
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
    """
    from vmware_avi.ops import lb_gate
    from vmware_avi.ops.vs_mgmt import toggle_vs
    from vmware_avi.ops.write_gate import resolve_confirm

    decision = resolve_confirm(confirm, confirmed=confirmed)
    return _gated("set_virtual_service_enabled", decision.deprecated, lambda: lb_gate.vs_toggle(
        name, enable, act=decision.act,
        apply=lambda: _capture_output(toggle_vs, name, enable=enable, skip_prompt=True),
    ))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def list_lb_pools(vs_filter: Optional[str] = None) -> str:
    """[READ] Discover pools on the Controller.

    Returns Name, member count, Enabled and short UUID per pool. Use this before
    list_lb_pool_members: pools are often named differently from the VS that use them.

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
def list_lb_pool_members(pool: str) -> str:
    """[READ] List the members of a pool.

    Returns Server IP, Port, Enabled and Ratio per member. Use before
    enable_lb_pool_member or disable_lb_pool_member; run list_lb_pools first for the pool
    name. Reports configured state only, not live health-monitor results.

    Args:
        pool: Exact pool name as shown by list_lb_pools — matched literally, not
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
def enable_lb_pool_member(pool: str, server: str) -> str:
    """[WRITE] Enable a pool member so it receives traffic again.

    Returns a one-line confirmation. Use list_lb_pool_members first to verify the server
    IP. The server must already belong to the pool — this adds nothing.

    Args:
        pool: Exact pool name as shown by list_lb_pools — matched literally, not
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
def disable_lb_pool_member(
    pool: str,
    server: str,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
) -> dict:
    """[WRITE] Disable a pool member with graceful drain — existing connections
    complete, no new traffic.

    Without confirm=True this only previews: it returns blast_radius (pool name
    and uuid, the member's IP/port/state, and how many members are enabled
    before and after) and changes nothing. Show that to the user and get their
    explicit decision. Do not set confirm=True on your own because the user
    asked earlier: they have not seen what it changes yet. An already disabled
    member returns action "noop".

    Refused with confirm=True: the pool's only enabled member (the pool would
    serve nothing — enable another first, or use set_virtual_service_enabled on purpose), an IP
    that matches more than one member, and a pool whose members cannot be read.
    Use for maintenance or rolling deployments; run list_lb_pool_members first for the
    server IP, enable_lb_pool_member to reverse it.

    Args:
        pool: Exact pool name as shown by list_lb_pools — matched literally, not
            fuzzily, and an unknown name is refused rather than ignored. Pools
            are often named differently from the Virtual Services that use
            them, so do not infer it from a VS name.
        server: Server IP address.
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
    """
    from vmware_avi.ops import lb_gate
    from vmware_avi.ops.pool_mgmt import toggle_pool_member
    from vmware_avi.ops.write_gate import resolve_confirm

    decision = resolve_confirm(confirm, confirmed=confirmed)
    return _gated("disable_lb_pool_member", decision.deprecated, lambda: (
        lb_gate.pool_member_disable(
            pool, server, act=decision.act,
            apply=lambda: _capture_output(
                toggle_pool_member, pool, server, enable=False, skip_prompt=True
            ),
        )
    ))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def list_ssl_certs() -> str:
    """[READ] List SSL/TLS certificates stored on the AVI Controller.

    Returns Name, Subject, Expiry and Type per certificate, in one call that
    cannot be paged or filtered. Use for inventory or a certificate's exact name
    — use check_ssl_cert_expiry instead for only the ones expiring soon.
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
def check_ssl_cert_expiry(days: int = 30) -> str:
    """[READ] Check which SSL certificates expire within N days (default 30).

    Returns name, expiry date and days remaining, soonest first. Use this
    instead of list_ssl_certs when you only want certificates near expiry. Expired
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
def get_virtual_service_analytics(vs_name: str) -> str:
    """[READ] Performance metrics for one Virtual Service over the last hour.

    Returns L4 (bandwidth, connections) and L7 (latency, % errors, responses)
    averages over a fixed window that cannot be changed (12 samples, 5 min
    apart). Empty output means no traffic, not an error. Use when get_virtual_service_status
    shows degraded health; get_virtual_service_error_logs gives per-request detail.

    Args:
        vs_name: Exact Virtual Service name, case-sensitive, from list_virtual_services.
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
def get_virtual_service_error_logs(vs_name: str, since: str = "1h") -> str:
    """[READ] Recent request error logs for one Virtual Service.

    Returns up to 50 lines — timestamp, HTTP status, URI path, client IP — for
    status 400 and above. Use this instead of get_virtual_service_analytics for per-request
    detail. An empty result may mean no errors, or capture disabled on the VS.

    Args:
        vs_name: Exact Virtual Service name, from list_virtual_services.
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
def list_service_engines() -> str:
    """[READ] List Service Engines (AVI data-plane VMs) on the Controller.

    Returns Name, management IP, status (e.g. OPER_UP) and SE Group per SE, in
    one call that cannot be paged or filtered. Use to inventory capacity or find
    an SE's name and IP — use get_service_engine_health instead for degraded VS health.
    """
    from vmware_avi.ops.se_mgmt import list_service_engines as _ops_list_service_engines

    return _capture_output(_ops_list_service_engines)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def get_service_engine_health() -> str:
    """[READ] Health of every Service Engine — operational status and VS counts.

    Returns name, operational state and the number of VSes placed on each SE.
    Use when VS health degrades to check if the issue is at the SE level;
    list_service_engines gives the management IP and SE Group,
    get_virtual_service_status the affected VS.
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
def get_ako_status(context: Optional[str] = None) -> str:
    """[READ] Check AKO (AVI Kubernetes Operator) pod status in Kubernetes.

    Returns pod name, phase, ready flag, restart count and namespace. First step
    for Ingress or LoadBalancer issues in Tanzu/K8s; follow with get_ako_logs when
    it is not Running. Looks in one context's AKO namespace only — run
    list_ako_clusters if not found.

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
def get_ako_logs(
    tail: int = 100, since: Optional[str] = None, context: Optional[str] = None
) -> str:
    """[READ] AKO pod logs — Ingress creation failures, sync errors, Controller
    connectivity.

    Returns raw log text, not a table. Use when get_ako_status shows the pod
    unhealthy or diff_ako_sync reports a missing Ingress. Only the running
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
def restart_ako(
    context: Optional[str] = None,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
) -> dict:
    """[WRITE] Restart the AKO pod by deleting it — its StatefulSet recreates it.

    Without confirm=True this only previews: it returns blast_radius (context,
    namespace, the pod's name, uid, phase and restarts, and the Ingresses whose
    programming pauses until the new pod is Running) and changes nothing. Show
    that to the user and get their explicit decision. Do not set confirm=True
    on your own because the user asked earlier: they have not seen what it
    changes yet. The pod deleted is the one measured (uid precondition).

    Refused with confirm=True: a pod already terminating, and a pod or Ingress
    list that cannot be read. Use when AKO is stuck or after config changes;
    brief traffic disruption is possible. Run get_ako_status afterwards, and
    get_ako_logs if the pod is not Running.

    Args:
        context: K8s context name (optional).
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
    """
    from vmware_avi.ops import ako_gate
    from vmware_avi.ops.ako_pod import restart_ako as _ops_restart_ako
    from vmware_avi.ops.write_gate import resolve_confirm

    decision = resolve_confirm(confirm, confirmed=confirmed)
    effect = ("Deletes the AKO pod; its StatefulSet recreates it. Ingress programming pauses "
              "until the new pod is Running; brief traffic disruption is possible.")
    return _gated("restart_ako", decision.deprecated, lambda: ako_gate.delete_ako_pod(
        "restart_ako", context, effect, act=decision.act,
        apply=lambda uid: _capture_output(_ops_restart_ako, context, skip_prompt=True, uid=uid),
    ))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def get_ako_version(context: Optional[str] = None) -> str:
    """[READ] AKO version running in a cluster, read from the pod's image tag.

    Returns the pod name and an Image/Version pair per container. Use it to
    check compatibility with the Controller, and before diff_ako_config or
    upgrade_ako_config so both target the installed chart. The tag is the only
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
def get_ako_config() -> str:
    """[READ] The AKO Helm release's values — controller IP, cloud name, network
    settings, feature flags.

    Returns YAML as helm reports it. Use this first to read the live config; use
    diff_ako_config instead to see what an upgrade would change. Only values
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
def diff_ako_config(chart_version: str = "") -> str:
    """[READ] Pending Helm value changes that have not been applied yet.

    Returns helm's diff output; empty means nothing would change. Credential
    values in it read `<redacted>` — that is this skill blanking them, not the
    configured value.
    Use this before upgrade_ako_config — it runs the same command, so the
    preview is real. Note: with chart_version empty the registry's moving latest
    is resolved, so two runs can differ with no local change; read get_ako_version
    and pass it to both.

    Args:
        chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest.
    """
    from vmware_avi.ops.ako_config import diff_ako_config as _ops_diff_ako_config

    return _capture_output(_ops_diff_ako_config, chart_version=chart_version)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
# Same helm output as diff_ako_config — see the note there.
@vmware_tool(risk_level="medium", sensitive_result=True)
def upgrade_ako_config(
    confirm: bool = False,
    chart_version: str = "",
    dry_run: Optional[bool] = None,
    confirmed: Optional[bool] = None,
) -> dict:
    """[WRITE] Apply an AKO Helm upgrade to the avi-system release.

    Finds the avi-system release automatically and upgrades the Broadcom OCI
    chart with --reuse-values. Without confirm=True this only previews: it
    returns blast_radius (release, the chart and app version it is on, revision
    and status, the chart it would move to) plus helm_dry_run, the output of
    `helm upgrade --dry-run`, and changes nothing. Show that to the user and get
    their explicit decision. Do not set confirm=True on your own because the
    user asked earlier: they have not seen what it changes yet.

    Refused with confirm=True: a failing dry-run (the real upgrade would fail
    too), a release with another helm operation pending, and a release whose
    status cannot be read. Helm output has credential values blanked to
    `<redacted>` by this skill. Run diff_ako_config first to review the change.

    Args:
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest,
            resolved at apply time, so it can differ from the preview.
        dry_run: Deprecated alias; removed in the next minor release. The old
            contract applied only with dry_run=false and confirmed=true;
            dry_run=true holds even when confirm=True.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
    """
    from vmware_avi.ops import ako_config, ako_gate
    from vmware_avi.ops.write_gate import resolve_confirm

    decision = resolve_confirm(confirm, confirmed=confirmed, dry_run=dry_run,
                               legacy_needs_dry_run_false=True)

    def apply() -> str:
        return _capture_output(
            ako_config.upgrade_ako, False, chart_version=chart_version, skip_prompt=True
        )

    return _gated("upgrade_ako_config", decision.deprecated,
                  lambda: ako_gate.upgrade(chart_version, act=decision.act, apply=apply))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def check_ako_ingress(namespace: str, context: Optional[str] = None) -> str:
    """[READ] Validate every Ingress in one namespace: IngressClass and TLS
    secret references that would stop AKO creating a Virtual Service.

    Returns name, IngressClass, issues and OK/ISSUES per Ingress. Run
    list_ako_ingresses first for namespace names; use diagnose_ako_ingress instead
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
def list_ako_ingresses(context: Optional[str] = None) -> str:
    """[READ] Inventory Kubernetes Ingresses across all namespaces.

    Returns Namespace, Ingress name, Host(s) and IngressClass per Ingress. Start
    here for namespace and Ingress names, then pass them to check_ako_ingress or
    diagnose_ako_ingress. Lists the K8s side only — use diff_ako_sync for
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
def diagnose_ako_ingress(
    name: str, namespace: str = "default", context: Optional[str] = None
) -> str:
    """[READ] Diagnose why one Ingress has no corresponding AVI Virtual Service.

    Validates IngressClass ('avi'/'avi-lb'), TLS secrets and backend Services.
    Returns annotations, a numbered issue list and kubectl fixes. Use
    list_ako_ingresses first to find Ingresses lacking a VS. Checks configuration
    only; when it is clean, try get_ako_logs and get_ako_sync_status.

    Args:
        name: Exact Ingress resource name.
        namespace: Namespace holding the Ingress (default 'default').
        context: kubeconfig context (optional), from list_ako_clusters.
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
def get_ako_sync_status(context: Optional[str] = None) -> str:
    """[READ] Compare the number of K8s Ingresses with the number of AVI Virtual
    Services.

    Returns both counts and a match/mismatch verdict. Use this first as a cheap
    check, then diff_ako_sync for which objects differ. A count comparison only
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
def diff_ako_sync(context: Optional[str] = None) -> str:
    """[READ] List Ingresses with no matching Virtual Service or pool on the
    Controller.

    Returns Type, namespace/name and Status per suspect Ingress. Use when
    get_ako_sync_status reports a mismatch; force_ako_sync reconciles. Shard-mode
    Ingresses are matched heuristically against AKO pool names, so confirm a
    'Missing' result with list_lb_pools before acting.

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
def force_ako_sync(
    context: Optional[str] = None,
    confirm: bool = False,
    confirmed: Optional[bool] = None,
) -> dict:
    """[WRITE] Force AKO to resync all K8s resources with the AVI Controller.

    The resync restarts the AKO pod so it rebuilds every Virtual Service, pool
    and VS-VIP from the cluster's current resources. Without confirm=True this
    only previews: it returns blast_radius (context, namespace, the pod's name,
    uid and phase, and the Ingresses it re-programs) and changes nothing. Show
    that to the user and get their explicit decision. Do not set confirm=True
    on your own because the user asked earlier: they have not seen what it
    changes yet.

    Refused with confirm=True: a pod already terminating, and a pod or Ingress
    list that cannot be read. Use when drift is detected; may cause brief
    traffic disruption. Run diff_ako_sync first to see what is out of sync,
    then get_ako_sync_status.

    Args:
        context: K8s context name (optional).
        confirm: False (default) returns the blast radius and changes nothing.
            True applies it.
        confirmed: Deprecated alias for confirm; removed in the next minor
            release. confirmed=False holds even when confirm=True.
    """
    from vmware_avi.ops import ako_gate
    from vmware_avi.ops.ako_sync import force_resync
    from vmware_avi.ops.write_gate import resolve_confirm

    decision = resolve_confirm(confirm, confirmed=confirmed)
    effect = ("Deletes the AKO pod to force a full resync: every Virtual Service, pool and VS-VIP "
              "is rebuilt from the cluster's resources; brief traffic disruption is possible.")
    return _gated("force_ako_sync", decision.deprecated, lambda: ako_gate.delete_ako_pod(
        "force_ako_sync", context, effect, act=decision.act,
        apply=lambda uid: _capture_output(force_resync, context, skip_prompt=True, uid=uid),
    ))


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
def list_ako_clusters() -> str:
    """[READ] List every Kubernetes context in the active kubeconfig and whether
    AKO is deployed there.

    Returns Context, AKO Status (pod phase or 'Not deployed') and Version per
    context. Requires kubectl on PATH; every context is probed, so unreachable
    clusters add latency. Start here for context names, then pass one to
    get_ako_status, get_ako_logs or diagnose_ako_ingress.
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
def get_ako_amko_status() -> str:
    """[READ] AMKO (AVI Multi-Cluster Kubernetes Operator) GSLB status.

    Returns raw kubectl output: the AMKO pods in avi-system, then the GSLBConfig
    YAML if one exists. Use this only for multi-cluster GSLB questions — for
    single-cluster AKO health use get_ako_status instead. Always reads the current
    kubectl context; see list_ako_clusters."""
    from vmware_avi.ops.ako_multi_cluster import show_amko_status

    return _capture_output(show_amko_status)


# ═══════════════════════════════════════════════════════════════════════════════
# Environment declaration
# ═══════════════════════════════════════════════════════════════════════════════

# The environment resolver lives in policy_environment so the CLI registers
# it too (its @guarded writes go through the same guard()); importing it here
# registers it for the MCP surface.
from vmware_avi.policy_environment import _cached_config, _environment_for  # noqa: E402,F401

# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main(
    expose: Optional[str] = None,
    allow_write: Optional[str] = None,
    transport: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    cert: Optional[str] = None,
    key: Optional[str] = None,
    public_name: Optional[str] = None,
    token_file: Optional[str] = None,
) -> None:
    """Entry point for vmware-avi-mcp."""
    logging.basicConfig(level=logging.INFO)
    from vmware_policy.serve import run_server
    run_server(
        mcp,
        skill="avi",
        expose=expose, allow_write=allow_write, transport=transport, host=host, port=port,
        cert=cert, key=key, public_name=public_name, token_file=token_file,
        # Both connection managers give each call its own session (HLD §16 D-13(2)).
        pooled=True,
    )


if __name__ == "__main__":
    main()

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)
