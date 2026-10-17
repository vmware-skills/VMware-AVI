"""AVI Controller connection management via avisdk.

Handles multi-controller connections with session reuse, plus centralized
HTTP error translation: avisdk does NOT raise on 4xx/5xx, so raw
``resp.json()`` reads silently render API errors as empty results. All ops
read/write paths should go through ``api_get`` / ``api_post`` / ``api_put``,
which translate non-2xx responses into teaching ``AviApiError`` exceptions
and retry transient gateway errors (502/503/504) exactly once.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import threading
import time
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, ClassVar

from vmware_policy.pool import SessionPool

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
    if 400 <= status_code < 500:
        # The Controller answered and rejected this request: connectivity is
        # fine, and doctor would report green (VMware-Monitor 2.4.1, same shape).
        return (
            "The Controller rejected the request — check this call's arguments "
            "against the values its Args section allows, then retry."
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


_session_serial = itertools.count(1)

#: Marks the avisdk ``sessionDict`` keys of pooled sessions (see _create_session).
_POOLED_KEY_MARK = "#vmware-avi-"
_cleanup_installed = False
_cleanup_lock = threading.Lock()


def _clean_inactive_unpooled_sessions() -> None:
    """avisdk's ``_clean_inactive_sessions``, skipping the sessions the pool owns.

    avisdk runs its cleanup inside every ``ApiSession()`` constructor: it walks
    the module-global ``sessionDict``, POSTs ``logout`` through each entry idle
    for 20 minutes and deletes the entry. Those entries include sessions resting
    idle in our pool, so one call's login logged out — through the very
    ``ApiSession`` object — a session the pool was handing to another call at the
    same moment, and that call later failed with ``KeyError: 'connected'`` when
    avisdk tried to re-authenticate on the deleted entry. The pool owns the
    lifetime of its sessions (it probes each one at checkout and logs out what it
    drops), so this leaves them alone; other entries are cleaned as avisdk would,
    with ``pop`` instead of a check-then-``del`` that races.
    """
    from avi.sdk import avi_api

    cache = avi_api.sessionDict  # read now: clear_cached_sessions() rebinds it
    expiry = avi_api.ApiSession.SESSION_CACHE_EXPIRY
    for key, entry in list(cache.items()):
        if _POOLED_KEY_MARK in key:
            continue
        idle = avi_api.avi_timedelta(datetime.utcnow() - entry["last_used"])  # avisdk's clock
        if idle < expiry:
            continue
        try:
            entry["api"].post("logout")
        except Exception as e:  # noqa: BLE001 — as avisdk: the session may be gone
            _log.debug("Logout of an inactive avisdk session failed: %s", e)
        cache.pop(key, None)


def _install_cleanup() -> None:
    """Replace avisdk's cleanup once per process (it is looked up on the class per call)."""
    global _cleanup_installed  # noqa: PLW0603
    with _cleanup_lock:
        if not _cleanup_installed:
            from avi.sdk.avi_api import ApiSession

            ApiSession._clean_inactive_sessions = staticmethod(_clean_inactive_unpooled_sessions)
            _cleanup_installed = True


def _controller_fingerprint(ctrl: ControllerConfig) -> tuple:
    """What a pooled session is built from; a change means its sessions are stale."""
    return (ctrl.host, ctrl.port, ctrl.username, ctrl.tenant, ctrl.api_version, ctrl.verify_ssl)


def _pool_key(ctrl: ControllerConfig) -> str:
    """Pool key: the controller name plus a digest of the settings its sessions use.

    Keyed by settings, a session is always built from exactly the config its key
    names, even while calls holding an old and a new config.yaml overlap.
    """
    digest = hashlib.sha256(repr(_controller_fingerprint(ctrl)).encode()).hexdigest()[:8]
    return f"{ctrl.name}@{digest}"


class AviConnectionManager:
    """Manages connections to multiple AVI Controllers.

    Sessions come from one process-wide :class:`vmware_policy.pool.SessionPool`
    (HLD §16 D-13(2)), shared by every manager instance: the ops layer builds a
    manager per call, and a per-instance pool would log in on every call and
    never log out. Inside an MCP call scope each call holds its own
    ``ApiSession`` per controller (up to ``VMWARE_MCP_POOL_SIZE``); outside one
    (CLI, stdio) every caller gets the controller's one dedicated session.

    Each pooled session is a separate login. avisdk's ``ApiSession.get_session``
    cannot give that: it returns the object cached in its module-global
    ``sessionDict`` under ``controller:username:port``, so every caller shared
    one ``ApiSession``. Even a directly constructed ``ApiSession`` reads its CSRF
    token and session cookie from ``sessionDict[self.key]`` on every request, so
    two objects with the same key share one login and overwrite each other on
    re-authentication. :meth:`_create_session` therefore gives each session its
    own ``key`` before it logs in.
    """

    _pool: ClassVar[SessionPool[ApiSession] | None] = None
    _pool_lock: ClassVar[threading.Lock] = threading.Lock()
    #: Pool key -> the config its sessions are built from. A key never changes
    #: config (the key carries a digest of it), so concurrent calls cannot race.
    _built_from: ClassVar[dict[str, ControllerConfig]] = {}

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @classmethod
    def _sessions(cls) -> SessionPool[ApiSession]:
        with cls._pool_lock:
            if cls._pool is None:
                cls._pool = SessionPool(
                    "avi",
                    create=cls._create_for,
                    is_alive=cls._is_alive,
                    close=cls._close_session,
                )
            return cls._pool

    @classmethod
    def _create_for(cls, key: str) -> ApiSession:
        with cls._pool_lock:
            ctrl = cls._built_from[key]
        connect_failures = _connect_failure_types()
        try:
            return cls._create_session(ctrl)
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

    @staticmethod
    def _is_alive(session: ApiSession) -> bool:
        # Same probe as before pooling: any answer means the session works
        # (avisdk re-authenticates a 401 itself); an exception means it is dead.
        session.get("cluster/runtime")
        return True

    @staticmethod
    def _close_session(session: ApiSession) -> None:
        try:
            # Controller logout is POST /logout (same as avisdk's own session
            # cleanup) — DELETE /logout is not a valid endpoint.
            session.post("logout")
        except Exception as e:  # noqa: BLE001 — the session may already be dead
            _log.debug("Logout POST failed: %s — dropping session locally anyway", e)
        session.delete_session()  # pops only this session's own key
        session.close()

    def connect(self, controller_name: str | None = None) -> ApiSession:
        """Connect to a controller by name, or the active controller."""
        ctrl = (
            self._config.get_controller(controller_name)
            if controller_name
            else self._config.active_controller
        )
        pool = self._sessions()
        key = _pool_key(ctrl)
        with self._pool_lock:
            self._built_from.setdefault(key, ctrl)
            stale = [k for k in self._built_from if k != key and self._name_of(k) == ctrl.name]
        for old in stale:
            pool.discard(old)  # config edited: sessions to the old settings go
        return pool.get(key)

    @staticmethod
    def _name_of(key: str) -> str:
        return key.rsplit("@", 1)[0]

    def disconnect(self, controller_name: str) -> None:
        """Log out the controller's sessions (busy ones when their calls finish)."""
        pool = self._sessions()
        with self._pool_lock:
            keys = [k for k in self._built_from if self._name_of(k) == controller_name]
        for key in keys:
            pool.discard(key)

    def disconnect_all(self) -> None:
        self._sessions().close_all()

    def list_controllers(self) -> list[str]:
        return [c.name for c in self._config.controllers]

    def list_connected(self) -> list[str]:
        return sorted({self._name_of(k) for k in self._sessions().keys()})

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

        _install_cleanup()
        # Not ApiSession.get_session: it hands every caller the one object it
        # caches per controller:username:port (see the class docstring). No lock
        # is held here: a login may retry for minutes against a dead Controller,
        # and each session only ever touches its own sessionDict key.
        session = ApiSession(
            controller_ip=controller_ip,
            username=ctrl.username,
            password=ctrl.password,
            api_version=ctrl.api_version,
            tenant=ctrl.tenant,
            port=ctrl.port,
            # avisdk defaults verify=False; honour the configured flag so TLS
            # verification (the documented default) is actually enforced.
            verify=ctrl.verify_ssl,
            lazy_authentication=True,  # log in below, under this session's own key
        )
        # avisdk keeps the login (CSRF token, session cookie) in its global
        # sessionDict under self.key and reads it from there on every request;
        # a key of its own makes this a login of its own.
        session.key = f"{session.key}{_POOLED_KEY_MARK}{next(_session_serial)}"
        try:
            session.authenticate_session()
        except BaseException:
            session.delete_session()
            session.close()
            raise
        return session
