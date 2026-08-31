"""AVI-specific regression evals.

Findings verified against avisdk 30.2.6 source (bundled in .venv), the
Broadcom AVI data-structure reference, and official AKO/AMKO helm chart
sources (2026-06 review). Each test cites the bug it prevents.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

AKO_OCI_CHART = "oci://projects.packages.broadcom.com/ako/helm-charts/ako"


# ── Finding 1 — invented metric ID l7_client.avg_resp_latency ────────────────


@pytest.mark.unit
class TestL7LatencyMetricId:
    """`l7_client.avg_resp_latency` does not exist in the AVI metrics
    catalogue; the real metric is `l7_client.avg_client_txn_latency`."""

    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_show_analytics_requests_valid_metric(
        self, mock_avi_session: MagicMock,
    ) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-vs", "uuid": "vs-abc",
        }
        mock_avi_session.post.return_value.json.return_value = {"series": []}

        from vmware_avi.ops.analytics import show_analytics
        show_analytics("web-vs")

        _, kwargs = mock_avi_session.post.call_args
        metric_id = kwargs["data"]["metric_requests"][0]["metric_id"]
        assert "l7_client.avg_client_txn_latency" in metric_id
        assert "avg_resp_latency" not in metric_id

    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_vs_status_reads_valid_metric_key(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-vs", "uuid": "vs-abc", "enabled": True,
        }
        mock_avi_session.get.return_value.json.return_value = {
            "runtime": {"oper_status": {"state": "OPER_UP"}},
            "metrics": {"l7_client.avg_client_txn_latency": 42.5},
        }
        from vmware_avi.ops.vs_mgmt import show_vs_status
        show_vs_status("web-vs")
        out = capsys.readouterr().out
        assert "42.5" in out, (
            "show_vs_status must read the real metric key "
            "l7_client.avg_client_txn_latency from inventory metrics"
        )

    def test_invented_metric_id_absent_from_source(self) -> None:
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for d in ("vmware_avi",)
            for p in (REPO_ROOT / d).rglob("*.py")
            if "__pycache__" not in p.parts and "avg_resp_latency" in p.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"invented metric ID 'avg_resp_latency' still referenced in: {offenders}"
        )


# ── Finding 2 — SE listing must use serviceengine-inventory ──────────────────


@pytest.mark.unit
class TestListServiceEnginesInventory:
    """The ServiceEngine CONFIG object has no `oper_status`; reading it from
    GET /serviceengine always rendered Status=N/A. The listing must use
    serviceengine-inventory (config + runtime merged)."""

    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_uses_inventory_endpoint_and_runtime_status(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        def _dispatch(path: str, *args, **kwargs) -> MagicMock:
            resp = MagicMock()
            if path == "serviceengine-inventory":
                resp.json.return_value = {"results": [
                    {
                        "uuid": "se-a",
                        "config": {
                            "name": "Avi-se-a",
                            "mgmt_vnic": {"vnic_networks": [
                                {"ip": {"ip_addr": {"addr": "10.0.0.11"}}},
                            ]},
                            "se_group_ref": "https://c/api/serviceenginegroup/seg-1",
                        },
                        "runtime": {"oper_status": {"state": "OPER_UP"}},
                    },
                ]}
            else:
                resp.json.return_value = {"results": []}
            return resp

        mock_avi_session.get.side_effect = _dispatch

        from vmware_avi.ops.se_mgmt import list_service_engines
        list_service_engines()

        paths = [c.args[0] for c in mock_avi_session.get.call_args_list]
        assert "serviceengine-inventory" in paths, (
            "list_service_engines must query serviceengine-inventory — the "
            "config object returned by GET /serviceengine has no oper_status"
        )
        assert "serviceengine" not in paths

        out = capsys.readouterr().out
        assert "Avi-se-a" in out
        assert "10.0.0.11" in out
        assert "OPER_UP" in out


# ── Finding 3 — VipSeAssigned has no uuid; derive from ref/url ───────────────


@pytest.mark.unit
class TestSeHealthUuidFromRef:
    """VipSeAssigned carries the SE as a `ref`/`url`, not a `uuid` field —
    relying on `uuid` alone made every VS count collapse to 0."""

    @staticmethod
    def _wire(mock_avi_session: MagicMock, se_entry: dict) -> None:
        def _dispatch(path: str, *args, **kwargs) -> MagicMock:
            resp = MagicMock()
            if path == "virtualservice-inventory":
                resp.json.return_value = {"results": [
                    {"uuid": "vs-1", "runtime": {"vip_summary": [
                        {"service_engine": [se_entry]},
                    ]}},
                ]}
            elif path == "serviceengine-inventory":
                resp.json.return_value = {"results": [
                    {
                        "uuid": "se-a",
                        "config": {"name": "Avi-se-a"},
                        "runtime": {"oper_status": {"state": "OPER_UP"}},
                    },
                ]}
            else:
                resp.json.return_value = {}
            return resp

        mock_avi_session.get.side_effect = _dispatch

    @pytest.mark.usefixtures("_patch_avi_connect")
    @pytest.mark.parametrize("se_entry", [
        {"ref": "https://ctrl/api/serviceengine/se-a#Avi-se-a"},
        {"url": "https://ctrl/api/serviceengine/se-a#Avi-se-a"},
        {"uuid": "se-a"},  # builds that inject uuid keep working
    ])
    def test_vs_count_derived(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
        se_entry: dict,
    ) -> None:
        self._wire(mock_avi_session, se_entry)
        from vmware_avi.ops.se_mgmt import check_se_health
        check_se_health()
        out = capsys.readouterr().out
        assert "Avi-se-a" in out and "VS count: 1" in out, (
            f"SE uuid must be derivable from {list(se_entry)} — got:\n{out}"
        )


# ── Finding 4 — AKO Helm release discovery + OCI chart ref ───────────────────


def _helm_run_factory(releases: list[dict], calls: list[list[str]]):
    """Build a subprocess.run replacement that answers helm list/get/upgrade."""

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["helm", "list"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(releases), stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run


@pytest.mark.unit
class TestAkoHelmRelease:
    """Official AKO installs use `helm install --generate-name` against the
    Broadcom OCI registry — the release is not named 'ako' and the chart ref
    `avi/ako` never existed."""

    def test_upgrade_uses_discovered_release_and_oci_chart(self) -> None:
        calls: list[list[str]] = []
        releases = [{"name": "ako-1716000000", "chart": "ako-1.12.1"}]
        with patch(
            "vmware_avi.ops.ako_config.subprocess.run",
            side_effect=_helm_run_factory(releases, calls),
        ):
            from vmware_avi.ops.ako_config import upgrade_ako
            upgrade_ako(dry_run=True)

        upgrade_cmds = [c for c in calls if c[:2] == ["helm", "upgrade"]]
        assert upgrade_cmds, f"no helm upgrade invoked — calls: {calls}"
        cmd = upgrade_cmds[0]
        assert "ako-1716000000" in cmd, (
            f"upgrade must target the discovered release, got: {cmd}"
        )
        assert AKO_OCI_CHART in cmd, (
            f"upgrade must use the official OCI chart ref, got: {cmd}"
        )
        assert "avi/ako" not in cmd

    def test_show_values_uses_discovered_release(self) -> None:
        calls: list[list[str]] = []
        releases = [{"name": "ako-1716000000", "chart": "ako-1.12.1"}]
        with patch(
            "vmware_avi.ops.ako_config.subprocess.run",
            side_effect=_helm_run_factory(releases, calls),
        ):
            from vmware_avi.ops.ako_config import show_ako_config
            show_ako_config()

        get_cmds = [c for c in calls if c[:3] == ["helm", "get", "values"]]
        assert get_cmds and "ako-1716000000" in get_cmds[0], (
            f"helm get values must target the discovered release: {calls}"
        )

    def test_diff_uses_discovered_release(self) -> None:
        calls: list[list[str]] = []
        releases = [{"name": "ako-1716000000", "chart": "ako-1.12.1"}]
        with patch(
            "vmware_avi.ops.ako_config.subprocess.run",
            side_effect=_helm_run_factory(releases, calls),
        ):
            from vmware_avi.ops.ako_config import diff_ako_config
            diff_ako_config()

        diff_cmds = [c for c in calls if c[:3] == ["helm", "diff", "upgrade"]]
        assert diff_cmds, f"no helm diff invoked — calls: {calls}"
        assert "ako-1716000000" in diff_cmds[0]
        assert "avi/ako" not in diff_cmds[0]

    def test_helm_list_failure_is_reported(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        def _run(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="connection refused",
            )

        with patch(
            "vmware_avi.ops.ako_config.subprocess.run", side_effect=_run,
        ):
            from vmware_avi.ops.ako_config import show_ako_config
            with pytest.raises(SystemExit):
                show_ako_config()
        out = capsys.readouterr().out
        assert "helm list failed" in out
        assert "connection refused" in out

    def test_no_release_returns_teaching_error(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        calls: list[list[str]] = []
        with patch(
            "vmware_avi.ops.ako_config.subprocess.run",
            side_effect=_helm_run_factory([], calls),
        ):
            from vmware_avi.ops.ako_config import upgrade_ako
            with pytest.raises(SystemExit):
                upgrade_ako(dry_run=True)
        out = capsys.readouterr().out
        assert "helm list" in out, (
            "missing-release error must teach the user how to inspect "
            f"installed releases — got:\n{out}"
        )


# ── Finding 5 — AMKO pod selector dual fallback ──────────────────────────────


@pytest.mark.unit
class TestAmkoSelector:
    """Official AMKO chart labels pods `app.kubernetes.io/name=amko`; the
    bare `app=amko` selector only matches legacy installs. Use the dual
    selector pattern (primary then fallback) like ako_pod does."""

    def test_primary_selector_then_fallback(self) -> None:
        calls: list[list[str]] = []

        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            # Empty result for every pod query → forces fallback path.
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "vmware_avi.ops.ako_multi_cluster.subprocess.run",
            side_effect=_run,
        ):
            from vmware_avi.ops.ako_multi_cluster import show_amko_status
            show_amko_status()

        selectors = [
            cmd[cmd.index("-l") + 1]
            for cmd in calls
            if "get" in cmd and "pods" in cmd and "-l" in cmd
        ]
        assert selectors and selectors[0] == "app.kubernetes.io/name=amko", (
            f"primary AMKO selector must be app.kubernetes.io/name=amko: {selectors}"
        )
        assert "app=amko" in selectors, (
            f"must fall back to legacy app=amko selector: {selectors}"
        )


# ── Finding 6 — logout must be POST, not DELETE ──────────────────────────────


@pytest.mark.unit
class TestLogoutIsPost:
    """avisdk's own cleanup does `session['api'].post('logout')` — DELETE
    /logout is not a valid Controller endpoint."""

    def test_disconnect_posts_logout_and_deletes_session(
        self, sample_config,
    ) -> None:
        from vmware_avi.connection import AviConnectionManager

        mgr = AviConnectionManager(sample_config)
        session = MagicMock()
        mgr._sessions["lab"] = session

        mgr.disconnect("lab")

        session.post.assert_called_once_with("logout")
        session.delete_session.assert_called_once_with()
        for call in session.delete.call_args_list:
            assert call.args[:1] != ("logout",), (
                "logout must not be sent as DELETE"
            )
        assert "lab" not in mgr._sessions

    def test_logout_failure_does_not_raise(self, sample_config) -> None:
        from vmware_avi.connection import AviConnectionManager

        mgr = AviConnectionManager(sample_config)
        session = MagicMock()
        session.post.side_effect = RuntimeError("controller gone")
        mgr._sessions["lab"] = session

        mgr.disconnect("lab")  # must not raise
        assert "lab" not in mgr._sessions


# ── Finding 7 — error-log filter must be ge(response_code,400) ───────────────


@pytest.mark.unit
class TestErrorLogFilter:
    """`ne(response_code,200)` flags every 2xx/3xx except exactly 200 as an
    'error' (204, 301, 304...). Errors are >= 400."""

    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_filter_is_ge_400(self, mock_avi_session: MagicMock) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-vs", "uuid": "vs-abc",
        }
        mock_avi_session.get.return_value.json.return_value = {"results": []}

        from vmware_avi.ops.analytics import show_error_logs
        show_error_logs("web-vs", since="1h")

        log_calls = [
            c for c in mock_avi_session.get.call_args_list
            if c.args and c.args[0] == "analytics/logs"
        ]
        assert log_calls, "show_error_logs must GET analytics/logs"
        params = log_calls[0].kwargs.get("params", {})
        assert params.get("filter") == "ge(response_code,400)", (
            f"error-log filter must be ge(response_code,400), got "
            f"{params.get('filter')!r}"
        )


# ── Finding 8 — dead KNOWN_AKO_ANNOTATIONS constant ──────────────────────────


@pytest.mark.unit
def test_known_ako_annotations_constant_removed() -> None:
    """KNOWN_AKO_ANNOTATIONS listed invented annotation names and was never
    referenced — it must be deleted, not kept as misleading documentation."""
    import vmware_avi.ops.ako_ingress as ako_ingress

    assert not hasattr(ako_ingress, "KNOWN_AKO_ANNOTATIONS")


# ── Finding 9 — AKO is a StatefulSet, not a Deployment ───────────────────────


@pytest.mark.unit
class TestAkoStatefulSetWording:
    """AKO ships as a StatefulSet (replica 1); 'Deployment will recreate it'
    is wrong and misleads users debugging pod ownership."""

    @staticmethod
    def _patched_k8s(mock_core, sample_config, module: str):
        return (
            patch(f"vmware_avi.ops.{module}.load_config",
                  return_value=sample_config),
            patch(f"vmware_avi.ops.{module}.K8sConnectionManager"),
        )

    def test_restart_ako_message(
        self, mock_k8s_core_v1: MagicMock, sample_config,
        capsys: pytest.CaptureFixture,
    ) -> None:
        pod = SimpleNamespace(metadata=SimpleNamespace(name="ako-0"))
        mock_k8s_core_v1.list_namespaced_pod.return_value.items = [pod]

        p_cfg, p_k8s = self._patched_k8s(
            mock_k8s_core_v1, sample_config, "ako_pod")
        with p_cfg, p_k8s as MockK8s:
            MockK8s.return_value.core_v1.return_value = mock_k8s_core_v1
            MockK8s.return_value.namespace = "avi-system"
            from vmware_avi.ops.ako_pod import restart_ako
            restart_ako(skip_prompt=True)

        out = capsys.readouterr().out
        assert "StatefulSet" in out
        assert "Deployment" not in out

    def test_force_resync_message(
        self, mock_k8s_core_v1: MagicMock, sample_config,
        capsys: pytest.CaptureFixture,
    ) -> None:
        pod = SimpleNamespace(metadata=SimpleNamespace(name="ako-0"))
        mock_k8s_core_v1.list_namespaced_pod.return_value.items = [pod]

        p_cfg, p_k8s = self._patched_k8s(
            mock_k8s_core_v1, sample_config, "ako_sync")
        with p_cfg, p_k8s as MockK8s:
            MockK8s.return_value.core_v1.return_value = mock_k8s_core_v1
            MockK8s.return_value.namespace = "avi-system"
            from vmware_avi.ops.ako_sync import force_resync
            force_resync(skip_prompt=True)

        out = capsys.readouterr().out
        assert "StatefulSet" in out
        assert "Deployment" not in out

    def test_deployment_wording_absent_from_source(self) -> None:
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for d in ("vmware_avi",)
            for p in (REPO_ROOT / d).rglob("*.py")
            if "__pycache__" not in p.parts
            and "Deployment will recreate" in p.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"'Deployment will recreate' wording still present in: {offenders}"
        )
