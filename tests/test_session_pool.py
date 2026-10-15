"""HLD §16 D-13(2): each MCP call holds its own Controller session and K8s client.

Runs through the real ``AviConnectionManager`` / ``K8sConnectionManager``; only
the session factories are replaced by fakes, so nothing touches the network.
The last tests go one level down, into avisdk itself and into the console
capture, the two pieces of shared state concurrent calls used to collide on.
"""

from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from vmware_policy.pool import call_scope

from vmware_avi.config import AkoConfig, AppConfig, ControllerConfig
from vmware_avi.connection import AviConnectionManager
from vmware_avi.k8s_connection import K8sConnectionManager

_WAIT = 5.0


class FakeSession:
    """Stands in for an avisdk ApiSession; counts concurrent users to catch sharing."""

    _ids = itertools.count(1)

    def __init__(self, ctrl: ControllerConfig) -> None:
        self.id = next(self._ids)
        self.controller = ctrl.name
        self.dead = False
        self.logged_out = self.deleted = self.closed = False
        self._users = 0
        self.max_users = 0
        self._lock = threading.Lock()

    def get(self, path: str, **_: Any) -> Any:
        if self.dead:
            raise ConnectionError("session expired")
        return SimpleNamespace(status_code=200)

    def post(self, path: str, **_: Any) -> None:
        if path == "logout":
            self.logged_out = True

    def delete_session(self) -> None:
        self.deleted = True

    def close(self) -> None:
        self.closed = True

    def begin(self) -> None:
        with self._lock:
            self._users += 1
            self.max_users = max(self.max_users, self._users)

    def end(self) -> None:
        with self._lock:
            self._users -= 1


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[FakeSession]:
    sessions: list[FakeSession] = []

    def factory(ctrl: ControllerConfig) -> FakeSession:
        session = FakeSession(ctrl)
        sessions.append(session)
        return session

    monkeypatch.setattr(AviConnectionManager, "_create_session", staticmethod(factory))
    monkeypatch.delenv("VMWARE_MCP_POOL_SIZE", raising=False)
    return sessions


def _config(**lab: Any) -> AppConfig:
    return AppConfig(
        controllers=(
            ControllerConfig(name="lab", host=lab.get("host", "10.0.0.1")),
            ControllerConfig(name="prod", host="10.0.0.2"),
        ),
        default_controller="lab",
    )


def _run_calls(*bodies: Any) -> list[Any]:
    """Run each body in its own thread inside its own call scope; return results."""
    results: list[Any] = [None] * len(bodies)
    errors: list[BaseException] = []

    def runner(i: int, body: Any) -> None:
        try:
            with call_scope():
                results[i] = body()
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, b)) for i, b in enumerate(bodies)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT * 2)
    assert not any(t.is_alive() for t in threads), "a call never finished"
    if errors:
        raise errors[0]
    return results


# ── AVI Controller sessions ──────────────────────────────────────────────


def test_concurrent_calls_on_one_controller_never_share_a_session(made: list[FakeSession]) -> None:
    both_hold = threading.Barrier(2, timeout=_WAIT)

    def call() -> FakeSession:
        # The ops layer builds a manager per call; the pool must still be shared.
        session = AviConnectionManager(_config()).connect("lab")
        session.begin()
        try:
            both_hold.wait()
            assert AviConnectionManager(_config()).connect("lab") is session
        finally:
            session.end()
        return session

    first, second = _run_calls(call, call)
    assert first is not second, "two running calls were handed the same ApiSession"
    assert all(s.max_users == 1 for s in made)


def test_a_long_call_leaves_the_other_session_usable(made: list[FakeSession]) -> None:
    long_holds, release_long = threading.Event(), threading.Event()

    def long_call() -> FakeSession:
        session = AviConnectionManager(_config()).connect()
        long_holds.set()
        assert release_long.wait(_WAIT)
        return session

    def short_call() -> FakeSession:
        assert long_holds.wait(_WAIT)
        try:
            return AviConnectionManager(_config()).connect()
        finally:
            release_long.set()

    long_session, short_session = _run_calls(long_call, short_call)
    assert short_session is not long_session


def test_calls_on_different_controllers_overlap(
    made: list[FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VMWARE_MCP_POOL_SIZE", "1")
    overlap = threading.Barrier(2, timeout=_WAIT)  # breaks if they cannot overlap

    def on(name: str) -> Any:
        def call() -> FakeSession:
            session = AviConnectionManager(_config()).connect(name)
            overlap.wait()
            return session

        return call

    lab, prod = _run_calls(on("lab"), on("prod"))
    assert (lab.controller, prod.controller) == ("lab", "prod")


def test_outside_a_call_scope_one_session_is_reused_across_managers(
    made: list[FakeSession],
) -> None:
    first = AviConnectionManager(_config()).connect("lab")
    assert AviConnectionManager(_config()).connect("lab") is first
    assert len(made) == 1


def test_dead_session_is_logged_out_and_replaced(made: list[FakeSession]) -> None:
    mgr = AviConnectionManager(_config())
    first = mgr.connect("lab")
    first.dead = True
    second = mgr.connect("lab")
    assert second is not first
    assert first.logged_out and first.deleted and first.closed
    assert not second.closed


def test_no_name_pools_under_the_active_controller(made: list[FakeSession]) -> None:
    mgr = AviConnectionManager(_config())
    session = mgr.connect()
    assert mgr.connect("lab") is session
    assert mgr.list_connected() == ["lab"]
    mgr.disconnect_all()
    assert session.logged_out and mgr.list_connected() == []


def test_edited_controller_config_replaces_its_sessions(made: list[FakeSession]) -> None:
    old = AviConnectionManager(_config()).connect("lab")
    new = AviConnectionManager(_config(host="10.0.0.9")).connect("lab")
    assert new is not old and old.logged_out


def test_each_pooled_session_is_its_own_avisdk_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """avisdk keeps the login in a module-global dict keyed controller:user:port.

    ``ApiSession.get_session`` returned one cached object per key, and any two
    sessions sharing a key share (and overwrite) one CSRF token and cookie.
    Through the real avisdk, with only the HTTP login answered by a fake.
    """
    import requests
    from avi.sdk import avi_api

    monkeypatch.setattr(avi_api, "sessionDict", {})
    monkeypatch.setenv("LAB_PASSWORD", "pw")
    logins = itertools.count(1)

    def login(self: Any, url: str, *a: Any, **k: Any) -> Any:
        n = next(logins)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"version": {}, "session_cookie_name": "sessionid"},
            cookies={"csrftoken": f"csrf-{n}", "sessionid": f"sid-{n}"},
        )

    monkeypatch.setattr(requests.Session, "post", login)
    ctrl = ControllerConfig(name="lab", host="10.0.0.1")
    one = AviConnectionManager._create_session(ctrl)
    two = AviConnectionManager._create_session(ctrl)
    try:
        assert one is not two and one.key != two.key
        tokens = {s._get_api_headers(None, None, 60, None, None)["X-CSRFToken"] for s in (one, two)}
        assert tokens == {"csrf-1", "csrf-2"}, "two pooled sessions share one Controller login"
        one.delete_session()
        assert two.key in avi_api.sessionDict, "closing one session must not end the other's login"
    finally:
        one.close()
        two.close()


# ── Kubernetes clients (AKO) ─────────────────────────────────────────────


@pytest.fixture
def kube(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    from kubernetes import config

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("fake")
    built: list[tuple[str, str, MagicMock]] = []

    def new_client(config_file: str, context: str) -> MagicMock:
        client = MagicMock(name=f"client-{context}-{len(built)}")
        built.append((config_file, context, client))
        return client

    def contexts(config_file: str) -> Any:
        return [{"name": "dev"}, {"name": "ops"}], {"name": "dev"}

    def load_kube_config(*a: Any, **k: Any) -> None:
        raise AssertionError("load_kube_config rewrites the process-global Configuration")

    monkeypatch.setattr(config, "new_client_from_config", new_client)
    monkeypatch.setattr(config, "list_kube_config_contexts", contexts)
    monkeypatch.setattr(config, "load_kube_config", load_kube_config)
    monkeypatch.delenv("VMWARE_MCP_POOL_SIZE", raising=False)
    return SimpleNamespace(
        path=kubeconfig,
        built=built,
        mgr=lambda: K8sConnectionManager(AkoConfig(kubeconfig=str(kubeconfig))),
    )


def test_concurrent_k8s_calls_get_their_own_client_per_context(kube: SimpleNamespace) -> None:
    both_hold = threading.Barrier(3, timeout=_WAIT)

    def on(context: str) -> Any:
        def call() -> Any:
            client = kube.mgr().get_client(context)
            both_hold.wait()
            return client

        return call

    dev_a, dev_b, ops = _run_calls(on("dev"), on("dev"), on("ops"))
    assert len({id(dev_a), id(dev_b), id(ops)}) == 3
    assert sorted(ctx for _, ctx, _ in kube.built) == ["dev", "dev", "ops"]
    assert {f for f, _, _ in kube.built} == {str(kube.path)}


def test_k8s_default_context_is_the_kubeconfig_current_one(kube: SimpleNamespace) -> None:
    mgr = kube.mgr()
    client = mgr.get_client()
    assert mgr.get_client("dev") is client
    assert [ctx for _, ctx, _ in kube.built] == ["dev"]


def test_edited_kubeconfig_rebuilds_the_client(kube: SimpleNamespace) -> None:
    import os

    mgr = kube.mgr()
    first = mgr.get_client("dev")
    stat = os.stat(kube.path)
    os.utime(kube.path, (stat.st_atime, stat.st_mtime + 10))
    second = mgr.get_client("dev")
    assert second is not first
    first.close.assert_called_once_with()


# ── console capture ──────────────────────────────────────────────────────


def test_concurrent_captures_keep_their_own_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two tool calls printing at once each get only their own lines back."""
    import sys

    from rich.console import Console

    from vmware_avi.mcp_server import server as srv

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "console", Console(), raising=False)
    # Interleaved on purpose: alice prints, bob's capture starts and prints,
    # alice prints again while bob's capture is still open, then bob.
    alice_started, bob_started, alice_done = threading.Event(), threading.Event(), threading.Event()
    steps = {
        "alice": (None, alice_started, bob_started, alice_done),
        "bob": (alice_started, bob_started, alice_done, None),
    }

    def op(tag: str) -> Any:
        wait_first, set_first, wait_second, set_second = steps[tag]

        def body() -> None:
            if wait_first:
                assert wait_first.wait(_WAIT)
            mod.console.print(f"{tag}-before")
            set_first.set()
            assert wait_second.wait(_WAIT)
            mod.console.print(f"{tag}-after")
            if set_second:
                set_second.set()

        body.__module__ = __name__
        return body

    out: dict[str, str] = {}

    def run(tag: str) -> None:
        out[tag] = srv._capture_output(op(tag))

    threads = [threading.Thread(target=run, args=(t,)) for t in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT * 2)
    assert out["alice"].split() == ["alice-before", "alice-after"]
    assert out["bob"].split() == ["bob-before", "bob-after"]


def test_pool_timeout_reaches_the_agent_with_its_remedy() -> None:
    from vmware_policy.pool import PoolTimeoutError

    from vmware_avi.mcp_server.server import _safe_error

    err = PoolTimeoutError(
        "All 2 avi sessions to target 'lab' stayed busy. Raise VMWARE_MCP_POOL_SIZE"
    )
    msg = _safe_error(err, "list_lb_pools")
    assert "VMWARE_MCP_POOL_SIZE" in msg and "'lab'" in msg
