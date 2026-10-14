"""AVI Controller connection management via avisdk.

Handles multi-controller connections with session reuse, plus centralized
HTTP error translation: avisdk does NOT raise on 4xx/5xx, so raw
``resp.json()`` reads silently render API errors as empty results. All ops
read/write paths should go through ``api_get`` / ``api_post`` / ``api_put``,
which translate non-2xx responses into teaching ``AviApiError`` exceptions
and retry transient gateway errors (502/503/504) exactly once.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avi.sdk.avi_api import ApiSession

from vmware_avi.config import AppConfig, ControllerConfig

_log = logging.getLogger("vmware-avi.connection")

# Transient gateway statuses worth a single lightweight retry.
_RETRYABLE_STATUSES = frozenset({502, 503, 504})
_RETRY_DELAY_SECONDS = 2.0

# Default page size for full-collection reads. AVI caps an unbounded GET, so a
# fixed page silently truncates large environments; api_get_all pages past it.
_DEFAULT_PAGE_SIZE = 200


class AviApiError(Exception):
    """An AVI Controller API call failed — carries status code, path, and a
    teaching hint so callers (and agents) know how to correct the request."""

    def __init__(self, message: str, *, status_code: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


@lru_cache(maxsize=1)
def _connect_failure_types() -> tuple[type[BaseException], ...]:
    """Exception types a failed ``ApiSession.get_session`` can raise.

    avisdk reaches the Controller through ``requests``, whose ``ConnectionError``
    is a ``RequestException`` → ``OSError`` and is **not** the builtin
    ``ConnectionError`` (check ``requests.exceptions.ConnectionError.__mro__``).
    Catching only the builtin meant the teaching message in ``connect`` had never
    once fired against a real Controller: refused connections, unresolvable
    hostnames and rejected self-signed certificates all arrive as the requests
    type, the last two included because ``SSLError`` and ``ConnectTimeout``
    subclass it.

    Resolved on first use rather than at import, because ``requests`` costs
    ~100ms to import and nothing on the CLI path needs it until a connection is
    actually made. Resolved into a real tuple rather than named inline in the
    ``except`` clause, so matching an exception cannot itself raise
    ``AttributeError`` (踩坑 #40).
    """
    try:
        from requests.exceptions import ConnectionError as RequestsConnectionError
    except ImportError:  # pragma: no cover — requests ships as an avisdk dependency
        return (ConnectionError,)
    return (ConnectionError, RequestsConnectionError)


def _controller_error(resp) -> str:
    """Return the Controller's own ``error`` field, or ``""`` if there is none.

    The raw response body is deliberately not read. ``AviApiError`` is on
    ``_safe_error``'s passthrough list, so whatever lands in its message reaches
    the agent verbatim, and a body is not ours to vouch for: it can be an HTML
    error page from a reverse proxy, a traceback, or a request echo carrying the
    URL. AVI's own JSON error payload is a single short field naming what it
    rejected, which is the only part worth forwarding.
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body simply has no field to read
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    return error.strip()[:120] if isinstance(error, str) else ""


def _hint_for_status(status_code: int, path: str) -> str:
    """Return a correction hint for a failed API call."""
    if status_code == 404:
        return (
            "Resource not found — run the matching list tool (list_virtual_services, "
            "list_lb_pools, list_ssl_certs, list_service_engines) to get the exact "
            "name/uuid, then retry."
        )
    if status_code in (401, 403):
        return (
            "Authentication/permission failure — check credentials in "
            "~/.vmware-avi/.env and the configured tenant."
        )
    if status_code in _RETRYABLE_STATUSES:
        return (
            "Controller temporarily unavailable (gateway error) — retry "
            "shortly or check Controller health with: vmware-avi doctor"
        )
    return (
        "Check the request parameters, then run 'vmware-avi doctor' to confirm the "
        "Controller is reachable and healthy."
    )


def _api_request(session: ApiSession, method: str, path: str, **kwargs):
    """Issue a request via avisdk and translate non-2xx into AviApiError.

    Retries exactly once on transient gateway errors (502/503/504), but only
    for GET — a non-idempotent POST/PUT may have already been applied when the
    gateway returned 5xx, so re-sending it could double-apply. Other 4xx/5xx
    are never retried. Responses without an integer ``status_code`` (e.g. test
    doubles returning dicts) pass through unchanged.
    """
    retried = False
    while True:
        resp = getattr(session, method)(path, **kwargs)
        status = getattr(resp, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return resp

        if status in _RETRYABLE_STATUSES and not retried and method.lower() == "get":
            retried = True
            _log.warning(
                "AVI API %s '%s' returned HTTP %d — retrying once in %.0fs",
                method.upper(),
                path,
                status,
                _RETRY_DELAY_SECONDS,
            )
            time.sleep(_RETRY_DELAY_SECONDS)
            continue

        # Hint before detail: the agent-facing wrapper truncates at 300
        # characters with no ellipsis, so anything the Controller contributes has
        # to sit where the cut can only eat it. Losing Controller prose costs
        # nothing; losing the correction costs the retry.
        detail = _controller_error(resp)
        raise AviApiError(
            f"AVI API {method.upper()} '{path}' failed with HTTP {status}. "
            f"{_hint_for_status(status, path)}"
            + (f" Controller reported: {detail}" if detail else ""),
            status_code=status,
            path=path,
        )


def api_get(session: ApiSession, path: str, **kwargs):
    """GET via avisdk with centralized error translation (see AviApiError)."""
    return _api_request(session, "get", path, **kwargs)


def api_post(session: ApiSession, path: str, **kwargs):
    """POST via avisdk with centralized error translation (see AviApiError)."""
    return _api_request(session, "post", path, **kwargs)


def api_put(session: ApiSession, path: str, **kwargs):
    """PUT via avisdk with centralized error translation (see AviApiError)."""
    return _api_request(session, "put", path, **kwargs)


def api_get_all(
    session: ApiSession,
    path: str,
    *,
    page_size: int = _DEFAULT_PAGE_SIZE,
    params: dict | None = None,
    **kwargs,
) -> list[dict]:
    """GET every page of an AVI collection, concatenating ``results``.

    A single collection GET returns at most one page, so callers that read the
    first page only (or pass a fixed ``page_size``) silently truncate — and any
    count/diff derived from that read is quietly wrong on Controllers holding
    more objects than one page. This walks ``page=1,2,...`` until a short page
    is returned, so tallies stay honest. Returns the flat list of result dicts;
    the per-item shape is identical to ``api_get(...).json()['results']``.
    """
    merged: list[dict] = []
    base = {k: str(v) for k, v in (params or {}).items()}
    base["page_size"] = str(page_size)
    page = 1
    while True:
        resp = api_get(session, path, params={**base, "page": str(page)}, **kwargs)
        body = resp.json() if hasattr(resp, "json") else resp
        results = (body or {}).get("results", []) or []
        merged.extend(results)
        if len(results) < page_size:
            break
        page += 1
    return merged


class AviConnectionManager:
    """Manages connections to multiple AVI Controllers."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._sessions: dict[str, ApiSession] = {}

    def connect(self, controller_name: str | None = None) -> ApiSession:
        """Connect to a controller by name, or the active controller."""
        ctrl = (
            self._config.get_controller(controller_name)
            if controller_name
            else self._config.active_controller
        )

        if ctrl.name in self._sessions:
            session = self._sessions[ctrl.name]
            try:
                session.get("cluster/runtime")
                return session
            except Exception:
                del self._sessions[ctrl.name]

        connect_failures = _connect_failure_types()
        try:
            session = self._create_session(ctrl)
        except connect_failures as exc:
            # The cause is chained for the server-side log and deliberately not
            # interpolated: a requests connection error carries the full
            # scheme://host:port/path and a TLS one quotes the certificate
            # subject, and this message passes through _safe_error verbatim.
            # Remedies lead so that an unusually long controller name truncates
            # itself rather than the advice.
            raise AviApiError(
                "AVI Controller unreachable. Check its host and port in "
                "~/.vmware-avi/config.yaml and its credentials in ~/.vmware-avi/.env, "
                "then run: vmware-avi doctor. If the Controller presents a self-signed "
                f"certificate, set verify_ssl: false on its entry. Controller: '{ctrl.name}'",
                path="login",
            ) from exc
        self._sessions[ctrl.name] = session
        return session

    def disconnect(self, controller_name: str) -> None:
        if controller_name in self._sessions:
            session = self._sessions[controller_name]
            try:
                # Controller logout is POST /logout (same as avisdk's own
                # session cleanup) — DELETE /logout is not a valid endpoint.
                session.post("logout")
            except Exception as e:
                _log.debug(
                    "Logout POST failed for controller '%s': %s — dropping session locally anyway",
                    controller_name,
                    e,
                )
            session.delete_session()
            del self._sessions[controller_name]

    def disconnect_all(self) -> None:
        for name in list(self._sessions):
            self.disconnect(name)

    def list_controllers(self) -> list[str]:
        return [c.name for c in self._config.controllers]

    def list_connected(self) -> list[str]:
        return list(self._sessions.keys())

    @staticmethod
    def _resolve_host(host: str) -> str:
        """Resolve a hostname/FQDN to an IPv4/IPv6 address.

        Returns the host unchanged if it is already an IP literal. Falls back
        to the original host if DNS resolution fails (avisdk will surface the
        network error downstream with its own message).

        Rationale: AVI Controller analytics endpoints validate the
        ``controller_ip`` header as an IP address literal and reject hostnames
        with "Invalid Controller IP6 Address: <hostname>". Resolving once at
        connect time lets users put FQDNs in config.yaml.
        """
        import ipaddress
        import socket

        try:
            ipaddress.ip_address(host)
            return host  # already an IP literal
        except ValueError:
            pass

        try:
            # getaddrinfo handles both IPv4 and IPv6; prefer IPv4 when available
            infos = socket.getaddrinfo(host, None)
            ipv4 = next((i for i in infos if i[0] == socket.AF_INET), None)
            return (ipv4 or infos[0])[4][0]
        except socket.gaierror as e:
            _log.warning("DNS resolution failed for %s: %s — using raw host", host, e)
            return host

    @classmethod
    def _create_session(cls, ctrl: ControllerConfig) -> ApiSession:
        from avi.sdk.avi_api import ApiSession

        controller_ip = cls._resolve_host(ctrl.host)
        if controller_ip != ctrl.host:
            _log.info(
                "Connecting to AVI Controller: %s (%s -> %s)",
                ctrl.name,
                ctrl.host,
                controller_ip,
            )
        else:
            _log.info("Connecting to AVI Controller: %s (%s)", ctrl.name, ctrl.host)

        return ApiSession.get_session(
            controller_ip=controller_ip,
            username=ctrl.username,
            password=ctrl.password,
            api_version=ctrl.api_version,
            tenant=ctrl.tenant,
            port=ctrl.port,
            # avisdk defaults verify=False; honour the configured flag so TLS
            # verification (the documented default) is actually enforced.
            verify=ctrl.verify_ssl,
        )
