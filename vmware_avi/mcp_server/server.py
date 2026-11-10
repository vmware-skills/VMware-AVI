"""MCP Server for VMware AVI — stdio transport.

Exposes 28 tools (22 read, 6 write) for AVI Controller + AKO K8s operations.
Entry point: vmware-avi-mcp (defined in pyproject.toml).
"""

import sys as _sys
import json
import logging
import re
import ssl
import threading
from contextvars import ContextVar
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from vmware_policy import (  # noqa: F401 — compatibility/runtime export
    describe_tool_parameters,
    report_tool_failure,
    sanitize,
    vmware_tool,
)
from vmware_policy.pool import PoolTimeoutError

from vmware_avi import __version__
from vmware_avi.config import (
    CONFIG_FILE,
    ConfigError,
    UnknownControllerError,
    load_config,
    resolve_config_path,
)
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

#: For an error about the request itself: the message already names the fix,
#: and doctor would report green (same shape as VMware-Monitor 2.4.1).
_FIX_IN_MESSAGE_HINT = "The error message says what to change; connectivity is not the problem."
#: A ValueError's text is usually this skill's own remedy, but not always: one
#: raised while parsing the server's answer names no fix. So it does not claim
#: connectivity is fine (independent review, 2026-10-18).
_CHECK_MESSAGE_HINT = (
    "The error message should say what to change; if it names no fix, run "
    "'vmware-avi doctor'."
)

#: ValueErrors whose text was not authored as a remedy: a body that was not JSON
#: (a reverse proxy answering HTML) or not UTF-8, and TLS errors that subclass
#: ValueError. Those are exactly what doctor checks.
_UNAUTHORED_VALUE_ERRORS = (ssl.SSLError, json.JSONDecodeError, UnicodeError)


def _is_request_error(exc: BaseException) -> bool:
    """True when the Controller is reachable and the request itself is wrong.

    A 4xx the Controller answered with (except 401/403, which are credentials),
    an unknown controller name, and the ValueErrors this skill raises to reject
    an argument. Transport failures (``AviApiError`` without a status), 5xx,
    unparseable bodies and anything unplanned are not, and keep the doctor hint.
    """
    if isinstance(exc, AviApiError):
        code = exc.status_code
        return code is not None and 400 <= code < 500 and code not in (401, 403)
    if isinstance(exc, UnknownControllerError):
        return True
    return isinstance(exc, ValueError) and not isinstance(exc, _UNAUTHORED_VALUE_ERRORS)


def _hint_for(exc: BaseException) -> str:
    if not _is_request_error(exc):
        return _DOCTOR_HINT
    if isinstance(exc, (AviApiError, UnknownControllerError)):
        return _FIX_IN_MESSAGE_HINT
    return _CHECK_MESSAGE_HINT


def _names_a_remedy(text: str) -> bool:
    """Whether an ops failure message already says what to run next.

    The ops layer's not-found messages name the MCP tool to list with
    (``list_lb_pools``), not a ``vmware-avi`` command, so checking for the CLI
    name alone appended the doctor hint to every one of them.
    """
    if "vmware-avi" in text:
        return True
    # FastMCP keeps the registry private; if that ever moves, fall back to the
    # doctor hint rather than replace the real error with an AttributeError.
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    return any(re.search(rf"\b{re.escape(name)}\b", text) for name in tools)


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


def _as_error(captured: str, detail: str = "", exc: Optional[BaseException] = None) -> str:
    """Render a failed run as a payload no reader can mistake for output.

    The ops layer reports failure the way a CLI does — print, then exit — so the
    useful teaching text is already in ``captured`` and is kept verbatim. What
    was missing is any marker that the run failed at all: without the prefix the
    model receives a red "not found" message as an ordinary successful result
    and reports it to the user as a finding (issue #31's failure mode).

    ``_DOCTOR_HINT`` is appended only when the failure is not about the request
    and the text names nothing to act on. A request error (``exc`` a 4xx, an
    unknown controller, a rejected argument) never gets it: connectivity is
    fine there and doctor would report green. When the ops message already says
    which tool to run, repeating a generic "run doctor" would bury the specific
    advice under worse advice.

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
    if exc is None:
        hint = None if _names_a_remedy(body) else _DOCTOR_HINT
    elif "vmware-avi" in body:
        hint = None
    else:
        hint = _hint_for(exc)
        hint = None if hint == _FIX_IN_MESSAGE_HINT else hint
    if hint:
        body = f"{body} {hint}".strip()
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
        return _as_error(buf.getvalue(), _safe_error(exc, getattr(func, "__name__", "?")), exc)
    finally:
        _capture_target.reset(token)

    return buf.getvalue()


def _gated(tool: str, run) -> dict:
    """Run one gated write (HLD §7) and shape every outcome as a dict.

    A refusal (``GateRefusedError``) keeps its teaching text and the blast
    radius it measured; any other failure goes through ``_safe_error``. Both
    come back as ``{"error": ...}``, which ``@vmware_tool`` audits as a
    failure.
    """
    from vmware_avi.ops.write_gate import GateRefusedError

    try:
        out = run()
    except GateRefusedError as exc:
        _log.info("Tool %s refused: %s", tool, exc)
        out = {"error": sanitize(str(exc), 1000), "blast_radius": exc.blast_radius}
    except Exception as exc:  # noqa: BLE001 — reduced to a safe string by _safe_error
        out = {"error": _safe_error(exc, tool), "hint": _hint_for(exc)}
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Traditional mode — AVI Controller
# ═══════════════════════════════════════════════════════════════════════════════




























# ═══════════════════════════════════════════════════════════════════════════════
# AKO mode — Kubernetes
# ═══════════════════════════════════════════════════════════════════════════════



from vmware_avi.mcp_server._tools_services import register_tools as _register_services

(
    list_virtual_services,
    get_virtual_service_status,
    set_virtual_service_enabled,
    list_lb_pools,
    list_lb_pool_members,
    enable_lb_pool_member,
    disable_lb_pool_member,
    list_ssl_certs,
    check_ssl_cert_expiry,
    get_virtual_service_analytics,
    get_virtual_service_error_logs,
    list_service_engines,
    get_service_engine_health,
    get_ako_status,
) = _register_services(mcp, _sys.modules[__name__])





























from vmware_avi.mcp_server._tools_operations import register_tools as _register_operations

(
    get_ako_logs,
    restart_ako,
    get_ako_version,
    get_ako_config,
    diff_ako_config,
    upgrade_ako_config,
    check_ako_ingress,
    list_ako_ingresses,
    diagnose_ako_ingress,
    get_ako_sync_status,
    diff_ako_sync,
    force_ako_sync,
    list_ako_clusters,
    get_ako_amko_status,
) = _register_operations(mcp, _sys.modules[__name__])


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
