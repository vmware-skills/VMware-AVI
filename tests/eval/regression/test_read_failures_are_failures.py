"""A read that failed is a failure, not "nothing there" (recurring error shape #1).

Found 2026-10-18 by the family isError gate's review: with no reachable
cluster, ``get_ako_amko_status`` answered "AMKO not deployed in avi-system
namespace." — kubectl had failed, not found nothing. The same shape was in
``list_ako_clusters`` (a context kubectl could not read showed "Not deployed")
and ``diagnose_ako_ingress`` (any error reading a backend Service, a 403 or a
dropped connection, was reported as "Backend service 'x' not found"; any error
reading the Ingress itself said the Ingress was not found).
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

import vmware_avi.ops.ako_ingress as ako_ingress
import vmware_avi.ops.ako_multi_cluster as amc

_FAIL = SimpleNamespace(returncode=1, stdout="",
                        stderr="The connection to the server localhost:8080 was refused")
_EMPTY = SimpleNamespace(returncode=0, stdout="", stderr="")


def _capture(module, fn, *args, **kwargs) -> tuple[str, BaseException | None]:
    buf = io.StringIO()
    with patch.object(module, "console", Console(file=buf, width=200)):
        try:
            fn(*args, **kwargs)
        except SystemExit as exc:
            return buf.getvalue(), exc
    return buf.getvalue(), None


# ── show_amko_status ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_amko_kubectl_failure_is_a_failure_not_not_deployed():
    with patch.object(amc.subprocess, "run", return_value=_FAIL):
        out, exited = _capture(amc, amc.show_amko_status)
    assert exited is not None and exited.code, "a failed read must fail the call"
    assert "not deployed" not in out.lower()
    assert "connection to the server" in out, "say why"


@pytest.mark.unit
def test_amko_one_selector_failing_is_still_a_failure():
    """The legacy selector query failing means nothing can be concluded."""
    with patch.object(amc.subprocess, "run", side_effect=[_EMPTY, _FAIL, _EMPTY]):
        out, exited = _capture(amc, amc.show_amko_status)
    assert exited is not None and exited.code
    assert "not deployed" not in out.lower()


@pytest.mark.unit
def test_amko_really_not_deployed_is_an_answer():
    """Control: kubectl answered, and there are no AMKO pods."""
    with patch.object(amc.subprocess, "run", return_value=_EMPTY):
        out, exited = _capture(amc, amc.show_amko_status)
    assert exited is None
    assert "AMKO not deployed" in out


@pytest.mark.unit
def test_gslbconfig_read_failure_is_said_not_hidden():
    pods = SimpleNamespace(returncode=0, stdout="amko-0  1/1 Running", stderr="")
    with patch.object(amc.subprocess, "run", side_effect=[pods, _FAIL]):
        out, exited = _capture(amc, amc.show_amko_status)
    assert exited is None
    assert "GSLBConfig" in out and "could not" in out.lower()


# ── list_clusters ────────────────────────────────────────────────────────


def _contexts(*names):
    return SimpleNamespace(returncode=0, stdout="\n".join(names), stderr="")


@pytest.mark.unit
def test_a_context_kubectl_cannot_read_is_not_not_deployed():
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("prod"), _FAIL]):
        out, exited = _capture(amc, amc.list_clusters)
    assert "Not deployed" not in out
    assert "Unreadable" in out or "could not" in out.lower()


@pytest.mark.unit
def test_a_context_without_ako_is_not_deployed():
    """Control: kubectl answered with no AKO pods (empty list, exit 0)."""
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("dev"), _EMPTY]):
        out, _ = _capture(amc, amc.list_clusters)
    assert "Not deployed" in out


@pytest.mark.unit
def test_the_pod_query_does_not_error_on_an_empty_list():
    """jsonpath {.items[0]...} errors on an empty list — that made "no pods" and
    "kubectl failed" look the same."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return _contexts("dev") if "config" in cmd else _EMPTY

    with patch.object(amc.subprocess, "run", side_effect=run):
        _capture(amc, amc.list_clusters)
    pod_query = next(c for c in calls if "pods" in c)
    assert "{.items[0]" not in " ".join(pod_query)


@pytest.mark.unit
def test_a_running_ako_is_shown_with_its_version():
    """Control: the output format a context with AKO still produces."""
    running = SimpleNamespace(returncode=0, stdout="Running:projects.registry/ako:1.12.1\n",
                              stderr="")
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("prod"), running]):
        out, _ = _capture(amc, amc.list_clusters)
    assert "Running" in out and "1.12.1" in out


# ── diagnose_ingress ─────────────────────────────────────────────────────


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"({status}) Reason: {'Not Found' if status == 404 else 'Forbidden'}")
        self.status = status


def _ingress_with_backend(svc: str):
    path = SimpleNamespace(backend=SimpleNamespace(service=SimpleNamespace(name=svc)))
    rule = SimpleNamespace(http=SimpleNamespace(paths=[path]))
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations={}),
        spec=SimpleNamespace(ingress_class_name="avi", tls=None, rules=[rule]),
    )


def _diagnose(read_ingress, read_service) -> tuple[str, BaseException | None]:
    k8s = MagicMock()
    k8s.core_v1.return_value.read_namespaced_service.side_effect = read_service
    net = MagicMock()
    net.read_namespaced_ingress.side_effect = read_ingress
    with patch.object(ako_ingress, "load_config", return_value=MagicMock()), \
         patch.object(ako_ingress.K8sConnectionManager, "from_config", return_value=k8s), \
         patch("kubernetes.client.NetworkingV1Api", return_value=net):
        return _capture(ako_ingress, ako_ingress.diagnose_ingress, "web", "default")


@pytest.mark.unit
def test_a_backend_read_that_is_forbidden_is_not_not_found():
    out, _ = _diagnose(lambda *a: _ingress_with_backend("api"), _ApiError(403))
    assert "Backend service 'api' not found" not in out
    assert "could not" in out.lower() and "403" in out


@pytest.mark.unit
def test_a_backend_that_is_really_missing_is_not_found():
    """Control: a 404 is the answer 'it is not there'."""
    out, _ = _diagnose(lambda *a: _ingress_with_backend("api"), _ApiError(404))
    assert "Backend service 'api' not found" in out


@pytest.mark.unit
def test_an_ingress_read_that_is_forbidden_says_so():
    out, exited = _diagnose(_ApiError(403), None)
    assert exited is not None and exited.code
    assert "not found in namespace" not in out
    assert "403" in out


@pytest.mark.unit
def test_an_ingress_that_is_really_missing_is_not_found():
    out, exited = _diagnose(_ApiError(404), None)
    assert exited is not None and exited.code
    assert "not found in namespace" in out


# ── review of 83b57e7 ────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_pod_jsonpath_has_kubectls_escaped_newline():
    """C1: a real newline inside the jsonpath quotes is rejected by kubectl
    ("unterminated quoted string"), which made every context "Unreadable"."""
    jp = amc._AKO_POD_JSONPATH
    assert "\n" not in jp
    assert '{"\\n"}' in jp


@pytest.mark.unit
def test_kubectl_accepts_the_pod_jsonpath(tmp_path):
    """The real parser, not a mock: client-side dry run, no cluster needed."""
    import shutil
    import subprocess

    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl not on PATH")
    r = subprocess.run(
        [kubectl, "create", "configmap", "probe", "--dry-run=client",
         "-o", amc._AKO_POD_JSONPATH],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "KUBECONFIG": str(tmp_path / "none"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.unit
def test_every_context_unreadable_fails_the_call():
    """I1: with nothing readable, the overview has no answer."""
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("a", "b"), _FAIL, _FAIL]):
        out, exited = _capture(amc, amc.list_clusters)
    assert exited is not None and exited.code
    assert "could not be read" in out


@pytest.mark.unit
def test_an_unreadable_context_shows_kubectls_reason():
    """I2: 'kubectl exit 1' alone cannot tell RBAC from a dead API server."""
    ok = SimpleNamespace(returncode=0, stdout="Running:ako:1.12.1\n", stderr="")
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("a", "b"), ok, _FAIL]):
        out, exited = _capture(amc, amc.list_clusters)
    assert exited is None, "a partial overview is still an answer"
    assert "connection to the server" in out


@pytest.mark.unit
def test_context_names_are_not_rich_markup():
    """S4: a '[/]' in a context name must not raise MarkupError or vanish."""
    with patch.object(amc.subprocess, "run", side_effect=[_contexts("weird[/]ctx", "ok"),
                                                          _FAIL, _EMPTY]):
        out, _ = _capture(amc, amc.list_clusters)
    assert "weird[/]ctx" in out


# ── same shape elsewhere (review I3, S3) ─────────────────────────────────


@pytest.mark.unit
def test_a_certificate_whose_expiry_cannot_be_read_is_counted_not_dropped():
    """'Could not read the expiry' used to become 'not expiring'."""
    import vmware_avi.ops.ssl_mgmt as ssl_mgmt

    certs = [
        {"name": "good", "certificate": {"not_after": "2099-01-01 00:00:00"}},
        {"name": "nodate", "certificate": {}},
        {"name": "odd", "certificate": {"not_after": "Jan 1 2099"}},
    ]
    with patch.object(ssl_mgmt, "load_config", return_value=MagicMock()), \
         patch.object(ssl_mgmt, "AviConnectionManager"), \
         patch.object(ssl_mgmt, "api_get_all", return_value=certs):
        out, _ = _capture(ssl_mgmt, ssl_mgmt.check_expiry, 30)
    assert "2 certificate(s)" in out and "could not be read" in out
    assert "nodate" in out and "odd" in out


@pytest.mark.unit
def test_helm_list_output_that_is_not_json_is_not_no_release():
    import vmware_avi.ops.ako_config as ako_config

    bad = SimpleNamespace(returncode=0, stdout="WARNING: something\n[", stderr="")
    with patch.object(ako_config.subprocess, "run", return_value=bad):
        out, exited = _capture(ako_config, ako_config._find_ako_release, "avi-system")
    assert exited is not None and exited.code
    assert "No AKO Helm release found" not in out
    assert "not JSON" in out or "could not be parsed" in out


@pytest.mark.unit
def test_a_failed_runtime_read_says_so():
    import vmware_avi.ops.vs_mgmt as vs_mgmt

    session = MagicMock()
    session.get_object_by_name.return_value = {"name": "web", "uuid": "u-1", "vip": []}
    mgr = MagicMock()
    mgr.connect.return_value = session
    with patch.object(vs_mgmt, "load_config", return_value=MagicMock()), \
         patch.object(vs_mgmt, "AviConnectionManager", return_value=mgr), \
         patch.object(vs_mgmt, "api_get", side_effect=RuntimeError("HTTP 500")):
        out, _ = _capture(vs_mgmt, vs_mgmt.show_vs_status, "web")
    assert "could not be read" in out.lower()


@pytest.mark.unit
def test_a_tls_secret_read_that_is_forbidden_is_not_missing():
    ing = _ingress_with_backend("api")
    ing.spec.tls = [SimpleNamespace(secret_name="tls-web")]
    k8s = MagicMock()
    k8s.core_v1.return_value.read_namespaced_secret.side_effect = _ApiError(403)
    net = MagicMock()
    net.read_namespaced_ingress.return_value = ing
    with patch.object(ako_ingress, "load_config", return_value=MagicMock()), \
         patch.object(ako_ingress.K8sConnectionManager, "from_config", return_value=k8s), \
         patch("kubernetes.client.NetworkingV1Api", return_value=net):
        out, _ = _capture(ako_ingress, ako_ingress.diagnose_ingress, "web", "default")
    assert "TLS secret 'tls-web' missing" not in out
    assert "TLS secret 'tls-web' could not be read" in out and "403" in out
