"""The blast-radius gate in front of the AKO writes (HLD §7).

``ako_restart`` and ``ako_sync_force`` both delete the AKO pod (its
StatefulSet recreates it); ``ako_config_upgrade`` runs ``helm upgrade``. This
module measures first, with the reads ``ako_pod``, ``ako_sync`` and
``ako_config`` already make, and refuses on a blocker or an unreadable field.

The executors are the CLI's (``restart_ako``, ``force_resync``,
``upgrade_ako``). The pod delete is handed the measured pod's uid as a
precondition, so the pod that is deleted is the pod that was measured — a
replacement that appeared in between is refused by the API server, not deleted.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from vmware_avi._safety import redact_text, sanitize
from vmware_avi.config import load_config
from vmware_avi.k8s_connection import K8sConnectionManager
from vmware_avi.ops.ako_config import AKO_OCI_CHART
from vmware_avi.ops.ako_pod import _get_ako_pod_name
from vmware_avi.ops.write_gate import (
    MAX_LISTED,
    PREVIEW_HINT,
    GateRefusedError,
    applied,
    preview,
    refusal,
)

#: The namespace ``ako_config.upgrade_ako`` targets when the MCP tool calls it.
UPGRADE_NAMESPACE = "avi-system"

#: Characters of helm's dry-run output returned in the preview (as before).
MAX_HELM_OUTPUT = 4000

_POD_NEXT = "Check the AKO pod with ako_status and the cluster with ako_clusters, then retry."


def _read(fn: Callable[[], Any]) -> tuple[Any, bool]:
    """``(value, ok)``. Any failure is unmeasured, which refuses — never a guess."""
    try:
        return fn(), True
    except Exception:  # noqa: BLE001 — an unreadable field refuses the write
        return None, False


def _k8s(context: str | None) -> tuple[Any, Any, str]:
    k8s = K8sConnectionManager.from_config(load_config())
    return k8s, k8s.core_v1(context), k8s.namespace


# ─── pod delete (restart / resync) ───────────────────────────────────────────


def measure_ako_pod(context: str | None, effect: str) -> dict:
    """The blast radius of deleting the AKO pod in ``context``."""
    k8s, v1, ns = _k8s(context)
    try:
        pod_name = _get_ako_pod_name(v1, ns)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None
    pod, pod_ok = _read(lambda: v1.read_namespaced_pod(pod_name, ns))
    uid, _ = _read(lambda: pod.metadata.uid) if pod_ok else (None, False)
    phase, _ = _read(lambda: pod.status.phase) if pod_ok else (None, False)
    cs, _ = _read(lambda: (pod.status.container_statuses or [None])[0]) if pod_ok else (None, False)
    terminating, term_ok = (
        _read(lambda: pod.metadata.deletion_timestamp is not None) if pod_ok else (None, False)
    )

    def _list_ingresses() -> list[str]:
        from kubernetes.client import NetworkingV1Api

        items = NetworkingV1Api(k8s.get_client(context)).list_ingress_for_all_namespaces().items
        return [f"{i.metadata.namespace}/{i.metadata.name}" for i in items]

    ingresses, ing_ok = _read(_list_ingresses)
    unmeasured = [
        f
        for f, ok in (
            ("pod", pod_ok),
            ("pod_uid", bool(uid)),
            ("ingresses", ing_ok),
            ("pod_terminating", term_ok),
        )
        if not ok
    ]
    blockers = []
    if terminating:
        blockers.append(
            f"The AKO pod {pod_name} is already terminating; deleting it again does nothing. "
            "Wait for the replacement and check it with ako_status."
        )
    radius = {
        "context": context or "(current kube-context)",
        "namespace": ns,
        "pod": sanitize(pod_name),
        "pod_uid": uid,
        "phase": sanitize(str(phase)) if phase else None,
        "ready": getattr(cs, "ready", None),
        "restarts": getattr(cs, "restart_count", None),
        "ingress_count": len(ingresses) if ing_ok else None,
        "ingresses": [sanitize(i) for i in (ingresses or [])[:MAX_LISTED]],
        "effect": effect,
        "blockers": blockers,
        "unmeasured": unmeasured,
    }
    return radius


def delete_ako_pod(
    tool: str, context: str | None, effect: str, *, act: bool, apply: Callable[[str], str]
) -> dict:
    """Preview, or run ``apply(uid)`` — the pod-deleting executor, pinned to the measured uid."""
    radius = measure_ako_pod(context, effect)
    if not act:
        return preview(radius, PREVIEW_HINT)
    reason = refusal(tool, f"AKO pod {radius['pod']}", radius, _POD_NEXT)
    if reason:
        raise GateRefusedError(reason, radius)
    return applied(apply(radius["pod_uid"]), "pod_deleted", radius)


# ─── helm upgrade ─────────────────────────────────────────────────────────────


def _helm(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    except FileNotFoundError:
        raise ValueError(
            "helm is not on PATH, so the AKO release cannot be measured and nothing was "
            "changed. Install helm on the machine running this MCP server."
        ) from None


def read_release(namespace: str) -> dict:
    """The AKO release row from ``helm list`` — the read ``_find_ako_release`` makes."""
    result = _helm(["helm", "list", "-n", namespace, "-o", "json"], 60)
    if result.returncode != 0:
        raise ValueError(
            f"helm list failed in namespace '{namespace}', so the AKO release cannot be "
            "measured and nothing was changed. Check that helm is installed and the "
            f"kube-context can reach the cluster: helm list -n {namespace}."
        )
    try:
        releases = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        releases = None
    if not isinstance(releases, list):
        raise ValueError("helm list returned output that is not a JSON list; nothing was changed.")
    for rel in releases:
        if isinstance(rel, dict) and str(rel.get("chart", "")).startswith("ako"):
            return rel
    raise ValueError(
        f"No AKO Helm release found in namespace '{namespace}'. Inspect releases with "
        f"helm list -n {namespace}; nothing was changed."
    )


def measure_upgrade(chart_version: str) -> tuple[dict, str]:
    """``(blast_radius, redacted helm --dry-run output)`` for the AKO upgrade."""
    rel = read_release(UPGRADE_NAMESPACE)
    release = str(rel.get("name") or "")
    status = str(rel.get("status") or "")
    cmd = [
        "helm",
        "upgrade",
        release,
        AKO_OCI_CHART,
        "-n",
        UPGRADE_NAMESPACE,
        "--reuse-values",
        "--dry-run",
    ]
    if chart_version:
        cmd += ["--version", chart_version]
    dry = _helm(cmd, 300)
    blockers = []
    if dry.returncode != 0:
        blockers.append(
            "helm upgrade --dry-run failed, so the real upgrade would fail too: "
            f"{sanitize(redact_text(dry.stderr or ''), 300)} Fix that first; "
            "ako_config_diff shows the pending change."
        )
    if status.startswith("pending"):
        blockers.append(
            f"The release is '{status}': another helm operation is in progress on it. "
            "Wait for it to finish (helm history) before upgrading."
        )
    radius = {
        "release": sanitize(release),
        "namespace": UPGRADE_NAMESPACE,
        "chart_now": sanitize(str(rel.get("chart") or "")) or None,
        "app_version_now": sanitize(str(rel.get("app_version") or "")) or None,
        "revision": rel.get("revision"),
        "status": sanitize(status) or None,
        "chart_to": chart_version or "registry latest (unpinned)",
        "effect": (
            "helm upgrade --reuse-values against this release: the AKO pod rolls and "
            "ingress programming pauses until the new pod is Running."
        ),
        "blockers": blockers,
        "unmeasured": [
            field
            for field, key in (("release", "name"), ("chart_now", "chart"), ("status", "status"))
            if not rel.get(key)
        ],
    }
    if not chart_version:
        radius = {
            **radius,
            "note": (
                "chart_version is empty, so the registry's latest is resolved at apply time and "
                "can differ from this preview. Pass the version from ako_version to pin it."
            ),
        }
    return radius, sanitize(redact_text(dry.stdout or ""), MAX_HELM_OUTPUT)


def upgrade(chart_version: str, *, act: bool, apply: Callable[[], str]) -> dict:
    """Preview, or re-measure and run ``apply`` (the helm upgrade executor)."""
    radius, dry_output = measure_upgrade(chart_version)
    if not act:
        return {**preview(radius, PREVIEW_HINT), "helm_dry_run": dry_output}
    reason = refusal(
        "ako_config_upgrade",
        f"AKO release '{radius['release']}'",
        radius,
        "Check it with helm list -n avi-system and retry.",
    )
    if reason:
        raise GateRefusedError(reason, radius)
    return applied(apply(), "upgraded", radius)
