"""Kubernetes connection management for AKO operations.

Uses kubeconfig for authentication, supports context switching.

Clients come from one process-wide :class:`vmware_policy.pool.SessionPool`
keyed by kubeconfig context (HLD §16 D-13(2)): inside an MCP call scope each
call holds its own ``ApiClient`` per context; outside one (CLI, stdio) callers
share the context's one dedicated client. Each client is built with
``config.new_client_from_config``, never ``config.load_kube_config``: the latter
rewrites the process-global default ``Configuration``, so two calls on different
contexts could each end up talking to the other's cluster.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from vmware_policy.pool import SessionPool

if TYPE_CHECKING:
    from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api

from vmware_avi.config import AkoConfig, AppConfig, load_config

_log = logging.getLogger("vmware-avi.k8s")


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


@dataclass(frozen=True)
class _K8sSession:
    """One pooled client and the kubeconfig state it was built from."""

    client: ApiClient
    kubeconfig: str
    mtime: float | None


class K8sConnectionManager:
    """Manages Kubernetes API connections for AKO operations."""

    _pool: ClassVar[SessionPool[_K8sSession] | None] = None
    _pool_lock: ClassVar[threading.Lock] = threading.Lock()
    #: Pool key -> (context, kubeconfig file) its clients are built from. The key
    #: names both, so it never changes meaning while calls overlap a config edit.
    _kubeconfig_for: ClassVar[dict[str, tuple[str, str]]] = {}

    def __init__(self, ako_config: AkoConfig) -> None:
        self._config = ako_config

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> K8sConnectionManager:
        cfg = config or load_config()
        return cls(cfg.ako)

    @classmethod
    def _clients(cls) -> SessionPool[_K8sSession]:
        with cls._pool_lock:
            if cls._pool is None:
                cls._pool = SessionPool(
                    "ako",
                    create=cls._create_for,
                    is_alive=cls._is_alive,
                    close=lambda s: s.client.close(),
                )
            return cls._pool

    @classmethod
    def _create_for(cls, key: str) -> _K8sSession:
        from kubernetes import config

        with cls._pool_lock:
            context, kubeconfig = cls._kubeconfig_for[key]
        mtime = _mtime(kubeconfig)
        client = config.new_client_from_config(config_file=kubeconfig, context=context)
        _log.info("Connected to K8s (kubeconfig=%s, context=%s)", kubeconfig, context)
        return _K8sSession(client, kubeconfig, mtime)

    @staticmethod
    def _is_alive(session: _K8sSession) -> bool:
        # A client holds no login to expire; it is stale when the kubeconfig it
        # was built from changed (edited, or a refreshed token written back), so
        # an edit takes effect on the next call, as when every call re-read it.
        return session.mtime == _mtime(session.kubeconfig)

    def _resolve_context(self, context: str | None) -> tuple[str, str]:
        kubeconfig = os.path.expanduser(self._config.kubeconfig)
        name = context or self._config.default_context
        if not name:
            from kubernetes import config

            _, active = config.list_kube_config_contexts(config_file=kubeconfig)
            name = active["name"]
        return name, kubeconfig

    def get_client(self, context: str | None = None) -> ApiClient:
        """Get the Kubernetes API client for a context (the kubeconfig's current one if none)."""
        name, kubeconfig = self._resolve_context(context)
        pool = self._clients()
        key = f"{name} ({kubeconfig})"
        with self._pool_lock:
            self._kubeconfig_for.setdefault(key, (name, kubeconfig))
            stale = [k for k, (ctx, kc) in self._kubeconfig_for.items() if ctx == name and k != key]
        for old in stale:
            pool.discard(old)  # the config now points at another kubeconfig file
        return pool.get(key).client

    def core_v1(self, context: str | None = None) -> CoreV1Api:
        from kubernetes.client import CoreV1Api

        return CoreV1Api(self.get_client(context))

    def apps_v1(self, context: str | None = None) -> AppsV1Api:
        from kubernetes.client import AppsV1Api

        return AppsV1Api(self.get_client(context))

    @property
    def namespace(self) -> str:
        return self._config.namespace
