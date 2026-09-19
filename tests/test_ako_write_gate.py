"""ako_restart, ako_sync_force and ako_config_upgrade behind the HLD §7 gate.

* L2 — a bare call previews: no pod is deleted, no real ``helm upgrade`` runs.
* L1 — the blast radius names the pod (with its uid) and the Ingresses whose
  programming pauses, or the release, the chart it is on and the chart it
  would move to.
* L3 — a terminating pod, an unreadable pod or Ingress list, a failing
  ``helm upgrade --dry-run`` or a release mid-operation refuse ``confirm=True``.

The pod delete carries a uid precondition so the pod deleted is the pod
measured.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from vmware_avi.mcp_server import server
from vmware_avi.ops import ako_gate, ako_pod, ako_sync

POD_TOOLS = ["ako_restart", "ako_sync_force"]


def _pod(uid: Any = "uid-1", terminating: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="ako-0",
            uid=uid,
            deletion_timestamp="2026-09-19T00:00:00Z" if terminating else None,
        ),
        status=SimpleNamespace(
            phase="Running", container_statuses=[SimpleNamespace(ready=True, restart_count=3)]
        ),
    )


def _ingress(ns: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(namespace=ns, name=name))


@pytest.fixture
def cluster(monkeypatch):
    """A cluster with one AKO pod and two Ingresses; returns the CoreV1 double."""

    def install(pod: Any = None, ingresses: Any = None) -> MagicMock:
        v1 = MagicMock()
        v1.list_namespaced_pod.return_value.items = [
            SimpleNamespace(metadata=SimpleNamespace(name="ako-0"))
        ]
        if isinstance(pod, BaseException):
            v1.read_namespaced_pod.side_effect = pod
        else:
            v1.read_namespaced_pod.return_value = _pod() if pod is None else pod
        monkeypatch.setattr(ako_gate, "_k8s", lambda _ctx: (MagicMock(), v1, "avi-system"))
        # The executors (restart_ako / force_resync) open their own client.
        conn = SimpleNamespace(core_v1=lambda _ctx=None: v1, namespace="avi-system")
        for executor in (ako_pod, ako_sync):
            monkeypatch.setattr(executor, "load_config", lambda: None)
            monkeypatch.setattr(
                executor, "K8sConnectionManager", SimpleNamespace(from_config=lambda _cfg: conn)
            )

        items = (
            [_ingress("shop", "web"), _ingress("shop", "api")] if ingresses is None else ingresses
        )

        class _Net:
            def __init__(self, _client):
                pass

            def list_ingress_for_all_namespaces(self):
                if isinstance(items, BaseException):
                    raise items
                return SimpleNamespace(items=items)

        monkeypatch.setattr("kubernetes.client.NetworkingV1Api", _Net)
        return v1

    return install


# ─── pod-deleting tools ───────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_bare_call_previews_and_deletes_nothing(cluster, tool):
    v1 = cluster()
    out = getattr(server, tool)()
    assert out["action"] == "preview"
    v1.delete_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_preview_states_the_pod_and_the_ingresses(cluster, tool):
    cluster()
    br = getattr(server, tool)(context="prod")["blast_radius"]
    assert br["pod"] == "ako-0" and br["pod_uid"] == "uid-1"
    assert br["namespace"] == "avi-system" and br["context"] == "prod"
    assert br["phase"] == "Running" and br["ready"] is True and br["restarts"] == 3
    assert br["ingress_count"] == 2
    assert br["ingresses"] == ["shop/web", "shop/api"]
    assert br["blockers"] == [] and br["unmeasured"] == []


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_confirm_deletes_the_measured_pod_once_with_a_uid_precondition(cluster, tool):
    v1 = cluster()
    out = getattr(server, tool)(confirm=True)
    assert out["action"] == "pod_deleted"
    assert out["blast_radius"]["ingress_count"] == 2
    v1.delete_namespaced_pod.assert_called_once()
    args, kwargs = v1.delete_namespaced_pod.call_args
    assert args == ("ako-0", "avi-system")
    assert kwargs["body"].preconditions.uid == "uid-1"


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_a_terminating_pod_is_refused(cluster, tool):
    v1 = cluster(pod=_pod(terminating=True))
    assert getattr(server, tool)()["blast_radius"]["blockers"]
    out = getattr(server, tool)(confirm=True)
    v1.delete_namespaced_pod.assert_not_called()
    assert "already terminating" in out["error"]


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_an_unreadable_ingress_list_is_refused(cluster, tool):
    v1 = cluster(ingresses=RuntimeError("403 forbidden"))
    out = getattr(server, tool)(confirm=True)
    v1.delete_namespaced_pod.assert_not_called()
    assert "could not read ingresses" in out["error"]


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_an_unreadable_pod_is_refused(cluster, tool):
    v1 = cluster(pod=RuntimeError("timeout"))
    out = getattr(server, tool)(confirm=True)
    v1.delete_namespaced_pod.assert_not_called()
    assert "could not read pod, " in out["error"]
    assert out["blast_radius"]["unmeasured"][0] == "pod"


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_a_pod_without_uid_is_refused(cluster, tool):
    v1 = cluster(pod=_pod(uid=None))
    out = getattr(server, tool)(confirm=True)
    v1.delete_namespaced_pod.assert_not_called()
    assert "pod_uid" in out["error"]


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_no_ako_pod_teaches(cluster, tool):
    v1 = cluster()
    v1.list_namespaced_pod.return_value.items = []
    out = getattr(server, tool)(confirm=True)
    assert "ako_clusters" in out["error"]
    v1.delete_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_legacy_confirmed_true_acts_and_is_flagged(cluster, tool):
    v1 = cluster()
    out = getattr(server, tool)(confirmed=True)
    v1.delete_namespaced_pod.assert_called_once()
    assert "confirmed is deprecated" in out["deprecated"]


@pytest.mark.parametrize("tool", POD_TOOLS)
def test_legacy_confirmed_false_holds_against_confirm(cluster, tool):
    v1 = cluster()
    out = getattr(server, tool)(confirm=True, confirmed=False)
    assert out["action"] == "preview"
    v1.delete_namespaced_pod.assert_not_called()


# ─── ako_config_upgrade ───────────────────────────────────────────────────────


RELEASE = {
    "name": "ako-1712",
    "chart": "ako-1.11.1",
    "app_version": "1.11.1",
    "revision": 4,
    "status": "deployed",
}


@pytest.fixture
def helm(monkeypatch):
    """Answer helm list / upgrade --dry-run; record every helm command; patch the executor."""

    def install(release: Any = RELEASE, dry_rc: int = 0) -> SimpleNamespace:
        state = SimpleNamespace(commands=[], applied=[])

        def fake(cmd, _timeout):
            state.commands.append(cmd)
            if cmd[1] == "list":
                import json

                return subprocess.CompletedProcess(cmd, 0, json.dumps([release]), "")
            assert "--dry-run" in cmd, f"a real helm upgrade ran: {cmd}"
            return subprocess.CompletedProcess(
                cmd, dry_rc, "MANIFEST\n  password: hunter2\n", "Error: chart not found"
            )

        def upgrade_ako(dry_run, *, chart_version="", skip_prompt=False):
            state.applied.append(
                {"dry_run": dry_run, "chart_version": chart_version, "skip_prompt": skip_prompt}
            )

        monkeypatch.setattr(ako_gate, "_helm", fake)
        monkeypatch.setattr("vmware_avi.ops.ako_config.upgrade_ako", upgrade_ako)
        return state

    return install


def test_upgrade_bare_call_previews_and_does_not_upgrade(helm):
    state = helm()
    out = server.ako_config_upgrade(chart_version="1.12.1")
    assert out["action"] == "preview"
    assert state.applied == []
    assert all("--dry-run" in c for c in state.commands if c[1] == "upgrade")


def test_upgrade_preview_states_release_and_chart_move(helm):
    helm()
    out = server.ako_config_upgrade(chart_version="1.12.1")
    br = out["blast_radius"]
    assert br["release"] == "ako-1712" and br["namespace"] == "avi-system"
    assert br["chart_now"] == "ako-1.11.1" and br["chart_to"] == "1.12.1"
    assert br["revision"] == 4 and br["status"] == "deployed"
    assert br["blockers"] == [] and br["unmeasured"] == []
    assert "MANIFEST" in out["helm_dry_run"]
    assert "hunter2" not in out["helm_dry_run"], "helm output must stay redacted"


def test_upgrade_unpinned_chart_says_so(helm):
    helm()
    br = server.ako_config_upgrade()["blast_radius"]
    assert br["chart_to"] == "registry latest (unpinned)"
    assert "note" in br


def test_upgrade_confirm_runs_the_executor_once(helm):
    state = helm()
    out = server.ako_config_upgrade(confirm=True, chart_version="1.12.1")
    assert out["action"] == "upgraded"
    assert state.applied == [{"dry_run": False, "chart_version": "1.12.1", "skip_prompt": True}]
    assert out["blast_radius"]["chart_now"] == "ako-1.11.1"


def test_upgrade_a_failing_dry_run_is_refused(helm):
    state = helm(dry_rc=1)
    out = server.ako_config_upgrade(confirm=True)
    assert state.applied == []
    assert "dry-run failed" in out["error"]


def test_upgrade_a_release_mid_operation_is_refused(helm):
    state = helm(release={**RELEASE, "status": "pending-upgrade"})
    out = server.ako_config_upgrade(confirm=True)
    assert state.applied == []
    assert "pending-upgrade" in out["error"]


def test_upgrade_a_release_without_status_is_refused(helm):
    state = helm(release={**RELEASE, "status": ""})
    out = server.ako_config_upgrade(confirm=True)
    assert state.applied == []
    assert "could not read status" in out["error"]


# The old contract acted only on dry_run=False AND confirmed=True.
@pytest.mark.parametrize(
    ("kwargs", "acts"),
    [
        ({"dry_run": False, "confirmed": True}, True),
        ({"confirmed": True}, False),
        ({"dry_run": False}, False),
        ({"dry_run": True, "confirmed": True}, False),
        ({"confirm": True, "dry_run": True}, False),
        ({"confirm": True, "confirmed": False}, False),
        ({"confirm": True, "dry_run": False}, True),
        ({"dry_run": True}, False),
    ],
    ids=[
        "old-act",
        "confirmed-alone",
        "dry-run-false-alone",
        "old-hold",
        "new-act-alias-holds",
        "disagree-confirmed",
        "new-act-alias-agrees",
        "old-default",
    ],
)
def test_upgrade_legacy_aliases_are_read_conservatively(helm, kwargs, acts):
    state = helm()
    out = server.ako_config_upgrade(**kwargs)
    assert bool(state.applied) is acts
    assert out["action"] == ("upgraded" if acts else "preview")
    legacy = [k for k in ("confirmed", "dry_run") if k in kwargs]
    for name in legacy:
        assert f"{name} is deprecated" in out["deprecated"]


def test_upgrade_schema():
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "ako_config_upgrade")
    props = tool.inputSchema["properties"]
    assert props["confirm"]["default"] is False
    assert props["confirmed"]["default"] is None
    assert props["dry_run"]["default"] is None


@pytest.mark.parametrize("tool_name", POD_TOOLS)
def test_pod_tool_schema(tool_name):
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == tool_name)
    props = tool.inputSchema["properties"]
    assert props["confirm"]["default"] is False
    assert props["confirmed"]["default"] is None


def test_upgrade_a_failed_helm_upgrade_is_an_error_not_a_success(helm, monkeypatch):
    helm()

    def failing(dry_run, *, chart_version="", skip_prompt=False):
        raise SystemExit(1)

    monkeypatch.setattr("vmware_avi.ops.ako_config.upgrade_ako", failing)
    out = server.ako_config_upgrade(confirm=True)
    assert "error" in out and "action" not in out
    assert out["blast_radius"]["release"] == "ako-1712"
