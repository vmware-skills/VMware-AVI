"""Regression evals for the 2026-06 external bug-report fix pack.

Each test pins one production bug:

1. K8sConnectionManager was constructed with a full AppConfig instead of an
   AkoConfig at 10 call sites — every AKO op crashed or read the wrong
   namespace. Tests use the REAL manager class (no class mock).
2. avisdk does not raise on 4xx/5xx — toggle_vs / toggle_pool_member printed
   unconditional success after failed writes.
3. Centralized HTTP error translation (CLAUDE.md 踩坑 #37): AviApiError with
   teaching hints, single retry on 502/503/504, no retry on 4xx, teaching
   error when the controller is unreachable.
4. MCP ako_config_upgrade used the interactive double-confirm prompt, which
   blocks on stdin and corrupts MCP stdio — replaced with a confirmed=False
   preview gate + skip_prompt path.
5. view_ako_logs / restart_ako / show_ako_version leaked RuntimeError when
   no AKO pod exists.
6. show_error_logs crashed slicing a null uri_path.
7. list_pools vs_filter missed pools because '#name' / '?...' fragments were
   left on refs.
9. CLI write commands never called the audit module.
10. FQDN→IP resolution + verify_ssl kwarg must reach ApiSession.get_session.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vmware_avi.config import AkoConfig, AppConfig, ConfigError, ControllerConfig
from vmware_avi.connection import (
    AviApiError,
    AviConnectionManager,
    api_get,
    api_post,
    api_put,
)
from vmware_avi.k8s_connection import K8sConnectionManager

# ── Fix 1 — K8sConnectionManager.from_config with a REAL manager ───────────


@pytest.mark.unit
class TestK8sManagerFromConfig:
    """No class mock: the real manager must accept AppConfig via from_config
    and expose the AKO namespace."""

    def test_from_config_namespace(self, sample_config: AppConfig) -> None:
        k8s = K8sConnectionManager.from_config(sample_config)
        assert k8s.namespace == "avi-system"

    def test_direct_init_takes_ako_config(self) -> None:
        k8s = K8sConnectionManager(AkoConfig(namespace="avi-system"))
        assert k8s.namespace == "avi-system"

    def test_check_ako_status_uses_real_manager(
        self, sample_config: AppConfig, capsys: pytest.CaptureFixture,
    ) -> None:
        """Exercise an AKO op through the REAL K8sConnectionManager class —
        only the K8s API method is stubbed, so a regression back to
        K8sConnectionManager(AppConfig) breaks `.namespace` and fails here."""
        cs = SimpleNamespace(restart_count=0, ready=True)
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="ako-0"),
            status=SimpleNamespace(phase="Running", container_statuses=[cs]),
            spec=SimpleNamespace(containers=[SimpleNamespace(image="ako:1.11.3")]),
        )
        v1 = MagicMock()
        v1.list_namespaced_pod.return_value.items = [pod]
        v1.read_namespaced_pod.return_value = pod

        with (
            patch("vmware_avi.ops.ako_pod.load_config", return_value=sample_config),
            patch.object(K8sConnectionManager, "core_v1", return_value=v1),
        ):
            from vmware_avi.ops.ako_pod import check_ako_status
            check_ako_status()

        v1.list_namespaced_pod.assert_called_once()
        assert v1.list_namespaced_pod.call_args[0][0] == "avi-system"


# ── Fix 3 — centralized error translation ──────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


@pytest.mark.unit
class TestApiErrorTranslation:
    def test_404_raises_teaching_error_with_list_hint(self) -> None:
        session = MagicMock()
        session.get.return_value = _FakeResp(404, "Not found")

        with pytest.raises(AviApiError) as exc_info:
            api_get(session, "virtualservice/vs-bogus")

        err = exc_info.value
        assert err.status_code == 404
        assert err.path == "virtualservice/vs-bogus"
        assert "list" in str(err).lower()
        session.get.assert_called_once()

    def test_503_retried_exactly_once_then_succeeds(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _FakeResp(503, "gateway error"),
            _FakeResp(200, payload={"results": []}),
        ]

        with patch("vmware_avi.connection.time.sleep") as mock_sleep:
            resp = api_get(session, "pool")

        assert resp.status_code == 200
        assert session.get.call_count == 2
        mock_sleep.assert_called_once()

    def test_503_twice_raises_after_single_retry(self) -> None:
        session = MagicMock()
        session.get.return_value = _FakeResp(503, "still down")

        with patch("vmware_avi.connection.time.sleep"):
            with pytest.raises(AviApiError) as exc_info:
                api_get(session, "pool")

        assert session.get.call_count == 2  # original + exactly one retry
        assert exc_info.value.status_code == 503

    def test_400_not_retried(self) -> None:
        session = MagicMock()
        session.put.return_value = _FakeResp(
            400, "bad payload", payload={"error": "Invalid value for field 'servers'"},
        )

        with patch("vmware_avi.connection.time.sleep") as mock_sleep:
            with pytest.raises(AviApiError) as exc_info:
                api_put(session, "pool/pool-1", data={})

        assert session.put.call_count == 1
        mock_sleep.assert_not_called()
        assert exc_info.value.status_code == 400
        # The Controller's own `error` field is forwarded; the raw body is not.
        # AviApiError is on _safe_error's passthrough list, so this message
        # reaches the agent verbatim and a body is not ours to vouch for.
        assert "Invalid value for field 'servers'" in str(exc_info.value)
        assert "bad payload" not in str(exc_info.value)

    def test_503_on_put_not_retried(self) -> None:
        """踩坑 #37: transient-5xx retry must be GET-only — re-sending a
        non-idempotent PUT after a gateway 503 (which may have already been
        applied) could double-apply, so a PUT 503 raises immediately."""
        session = MagicMock()
        session.put.return_value = _FakeResp(503, "gateway error")

        with patch("vmware_avi.connection.time.sleep") as mock_sleep:
            with pytest.raises(AviApiError) as exc_info:
                api_put(session, "pool/pool-1", data={})

        assert session.put.call_count == 1  # no retry
        mock_sleep.assert_not_called()
        assert exc_info.value.status_code == 503

    def test_503_on_post_not_retried(self) -> None:
        """Same GET-only retry contract for POST."""
        session = MagicMock()
        session.post.return_value = _FakeResp(503, "gateway error")

        with patch("vmware_avi.connection.time.sleep") as mock_sleep:
            with pytest.raises(AviApiError) as exc_info:
                api_post(session, "analytics/metrics/collection", data={})

        assert session.post.call_count == 1  # no retry
        mock_sleep.assert_not_called()
        assert exc_info.value.status_code == 503

    def test_503_on_get_retried_exactly_once(self) -> None:
        """Counterpart: a GET 503 IS retried exactly once (call count == 2)."""
        session = MagicMock()
        session.get.return_value = _FakeResp(503, "gateway error")

        with patch("vmware_avi.connection.time.sleep") as mock_sleep:
            with pytest.raises(AviApiError) as exc_info:
                api_get(session, "pool")

        assert session.get.call_count == 2  # original + exactly one retry
        mock_sleep.assert_called_once()
        assert exc_info.value.status_code == 503

    def test_unreachable_controller_teaching_error(
        self, sample_config: AppConfig,
    ) -> None:
        mgr = AviConnectionManager(sample_config)
        with patch.object(
            AviConnectionManager,
            "_create_session",
            side_effect=ConnectionError("connection refused"),
        ):
            with pytest.raises(AviApiError) as exc_info:
                mgr.connect()

        msg = str(exc_info.value)
        assert "unreachable" in msg
        assert "vmware-avi doctor" in msg

    def test_avisdk_connection_failure_reaches_the_teaching_error(
        self, sample_config: AppConfig,
    ) -> None:
        """The catch named the builtin ``ConnectionError``; avisdk never raises it.

        avisdk reaches the Controller through ``requests``, whose
        ``ConnectionError`` is a ``RequestException`` → ``OSError`` and is not a
        builtin ``ConnectionError`` at all. So the teaching message had never
        fired against a real Controller, and the test above passed only because
        it raised the builtin type by hand — the defect could not appear in the
        environment that was checking for it.
        """
        import requests

        mgr = AviConnectionManager(sample_config)
        raw = requests.exceptions.SSLError(
            "HTTPSConnectionPool(host='avi.internal', port=443): Max retries exceeded "
            "with url: /login (Caused by SSLCertVerificationError(1, \"hostname "
            "'avi.internal' doesn't match 'CN=avi.corp.example'\"))"
        )
        with patch.object(AviConnectionManager, "_create_session", side_effect=raw):
            with pytest.raises(AviApiError) as exc_info:
                mgr.connect()

        msg = str(exc_info.value)
        assert "unreachable" in msg
        assert "verify_ssl: false" in msg  # the self-signed-certificate remedy
        assert sample_config.controllers[0].name in msg
        # None of what the raw error carries: this message passes through
        # _safe_error verbatim, and sanitize truncates without redacting.
        assert "avi.internal" not in msg
        assert "avi.corp.example" not in msg
        assert "Max retries" not in msg

    def test_missing_password_is_not_reported_as_unreachable(
        self, sample_config: AppConfig,
    ) -> None:
        """``ConfigError`` subclasses ``OSError``, so widening this catch to the
        base class would swallow the family's most common first-run failure and
        replace its remedy — the env var name — with 'Controller unreachable'."""
        mgr = AviConnectionManager(sample_config)
        with patch.object(
            AviConnectionManager,
            "_create_session",
            side_effect=ConfigError(
                "Password not found. Add it to ~/.vmware-avi/.env (chmod 600), or "
                "export the environment variable LAB_PASSWORD"
            ),
        ):
            with pytest.raises(ConfigError) as exc_info:
                mgr.connect()

        assert "LAB_PASSWORD" in str(exc_info.value)


# ── Fix 2 — write failures must not print success ──────────────────────────


@pytest.mark.unit
class TestWriteFailureReported:
    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_toggle_vs_put_failure(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-vs", "uuid": "vs-1", "enabled": False,
        }
        mock_avi_session.put.return_value = _FakeResp(400, "version conflict")

        from vmware_avi.ops.vs_mgmt import toggle_vs
        with pytest.raises(SystemExit):
            toggle_vs("web-vs", enable=True)

        out = capsys.readouterr().out
        assert "Failed to enable" in out
        assert "enabled." not in out  # no unconditional success line

    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_toggle_pool_member_put_failure(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-pool", "uuid": "pool-1",
            "servers": [{"ip": {"addr": "10.0.0.9"}, "enabled": False}],
        }
        mock_avi_session.put.return_value = _FakeResp(400, "bad request")

        from vmware_avi.ops.pool_mgmt import toggle_pool_member
        with pytest.raises(SystemExit):
            toggle_pool_member("web-pool", "10.0.0.9", enable=True)

        out = capsys.readouterr().out
        assert "Failed to enable" in out


# ── Fix 4 — MCP upgrade preview gate (no stdin prompt over MCP stdio) ──────


@pytest.mark.unit
class TestMcpUpgradeGate:
    def test_not_confirmed_returns_preview_without_running(self) -> None:
        from vmware_avi.mcp_server import server

        with patch("vmware_avi.ops.ako_config.upgrade_ako") as mock_upgrade:
            out = server.ako_config_upgrade(dry_run=False, confirmed=False)

        mock_upgrade.assert_not_called()
        assert "[preview]" in out
        assert "confirmed=True" in out

    def test_confirmed_runs_with_skip_prompt(self) -> None:
        from vmware_avi.mcp_server import server

        with patch("vmware_avi.ops.ako_config.upgrade_ako") as mock_upgrade:
            server.ako_config_upgrade(dry_run=False, confirmed=True)

        mock_upgrade.assert_called_once()
        assert mock_upgrade.call_args.kwargs.get("skip_prompt") is True

    def test_dry_run_needs_no_confirmation(self) -> None:
        from vmware_avi.mcp_server import server

        with patch("vmware_avi.ops.ako_config.upgrade_ako") as mock_upgrade:
            server.ako_config_upgrade(dry_run=True, confirmed=False)

        mock_upgrade.assert_called_once()

    def test_upgrade_ako_skip_prompt_bypasses_double_confirm(self) -> None:
        ok = SimpleNamespace(returncode=0, stdout="upgraded", stderr="")
        with (
            patch("vmware_avi.ops.ako_config._find_ako_release", return_value="ako-1"),
            patch("vmware_avi.ops.ako_config.subprocess.run", return_value=ok),
            patch("vmware_avi._safety.double_confirm") as mock_confirm,
        ):
            from vmware_avi.ops.ako_config import upgrade_ako
            upgrade_ako(dry_run=False, skip_prompt=True)

        mock_confirm.assert_not_called()


# ── Fix 5 — RuntimeError from missing AKO pod handled gracefully ──────────


@pytest.mark.unit
class TestMissingAkoPodHandled:
    @pytest.fixture()
    def _empty_v1(self) -> MagicMock:
        v1 = MagicMock()
        v1.list_namespaced_pod.return_value.items = []
        return v1

    @pytest.mark.parametrize("op_name", ["view_ako_logs", "restart_ako", "show_ako_version"])
    def test_no_pod_exits_cleanly(
        self,
        op_name: str,
        _empty_v1: MagicMock,
        sample_config: AppConfig,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import vmware_avi.ops.ako_pod as ako_pod

        kwargs = {"skip_prompt": True} if op_name == "restart_ako" else {}
        with (
            patch("vmware_avi.ops.ako_pod.load_config", return_value=sample_config),
            patch.object(K8sConnectionManager, "core_v1", return_value=_empty_v1),
        ):
            with pytest.raises(SystemExit) as exc_info:
                getattr(ako_pod, op_name)(**kwargs)

        assert exc_info.value.code == 1
        assert "AKO pod" in capsys.readouterr().out


# ── Fix 6 — null log-record fields must not crash error log rendering ─────


@pytest.mark.unit
class TestNullLogFields:
    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_null_uri_path_rendered(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        mock_avi_session.get_object_by_name.return_value = {
            "name": "web-vs", "uuid": "vs-1",
        }
        mock_avi_session.get.return_value = _FakeResp(200, payload={
            "results": [{
                "report_timestamp": None,
                "response_code": 502,
                "uri_path": None,       # L4 records have no URI
                "client_ip": None,
            }],
        })

        from vmware_avi.ops.analytics import show_error_logs
        show_error_logs("web-vs", "1h")  # must not raise TypeError

        out = capsys.readouterr().out
        assert "502" in out
        assert "None" not in out


# ── Fix 7 — '#name' / '?...' fragments stripped from VS pool refs ─────────


@pytest.mark.unit
class TestPoolRefFragmentStrip:
    @pytest.mark.usefixtures("_patch_avi_connect")
    def test_filter_matches_ref_with_fragment(
        self, mock_avi_session: MagicMock, capsys: pytest.CaptureFixture,
    ) -> None:
        def _get(path: str, **kwargs):
            if path == "virtualservice-inventory":
                return _FakeResp(200, payload={"results": [{
                    "config": {"name": "web-vs"},
                    "pools": ["https://avi/api/pool/pool-uuid-1#web-pool"],
                    "poolgroups": [],
                }]})
            return _FakeResp(200, payload={"results": [
                {"name": "web-pool", "uuid": "pool-uuid-1", "servers": [{}], "enabled": True},
            ]})

        mock_avi_session.get.side_effect = _get

        from vmware_avi.ops.pool_mgmt import list_pools
        list_pools(vs_filter="web")

        out = capsys.readouterr().out
        assert "web-pool" in out
        assert "Showing 1 of 1" in out


# ── Fix 9 — CLI writes are audited ─────────────────────────────────────────


@pytest.mark.unit
class TestCliAuditWiring:
    def test_vs_enable_writes_audit_entry(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from vmware_avi import cli

        audit_file = tmp_path / "audit.log"
        with (
            patch("vmware_avi.ops.vs_mgmt.toggle_vs"),
            patch("vmware_avi.notify.audit.AUDIT_LOG", audit_file),
        ):
            result = CliRunner().invoke(cli.app, ["vs", "enable", "web-vs"])

        assert result.exit_code == 0, result.output
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["operation"] == "vs_enable"
        assert entry["resource"] == "web-vs"
        assert entry["result"] == "success"

    def test_audit_failure_never_blocks(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from vmware_avi import cli

        with (
            patch("vmware_avi.ops.vs_mgmt.toggle_vs"),
            patch(
                "vmware_avi.notify.audit.log_operation",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            result = CliRunner().invoke(cli.app, ["vs", "enable", "web-vs"])

        assert result.exit_code == 0, result.output


# ── Fix 10 — FQDN resolution + verify_ssl wiring ───────────────────────────


@pytest.mark.unit
class TestConnectionSessionKwargs:
    def test_fqdn_resolved_to_ip(self) -> None:
        import socket

        infos = [(socket.AF_INET, None, None, "", ("192.0.2.10", 0))]
        with patch("socket.getaddrinfo", return_value=infos):
            resolved = AviConnectionManager._resolve_host("avi.example.com")
        assert resolved == "192.0.2.10"

    def test_ip_literal_skips_dns(self) -> None:
        with patch("socket.getaddrinfo") as mock_gai:
            resolved = AviConnectionManager._resolve_host("10.0.0.1")
        assert resolved == "10.0.0.1"
        mock_gai.assert_not_called()

    def test_verify_ssl_kwarg_reaches_get_session(self) -> None:
        ctrl = ControllerConfig(name="lab", host="10.0.0.1", verify_ssl=False)
        with (
            patch("avi.sdk.avi_api.ApiSession.get_session") as mock_get_session,
            patch.dict("os.environ", {"LAB_PASSWORD": "pw"}),
        ):
            AviConnectionManager._create_session(ctrl)

        kwargs = mock_get_session.call_args.kwargs
        assert kwargs["verify"] is False
        assert kwargs["controller_ip"] == "10.0.0.1"

    def test_verify_ssl_default_true(self) -> None:
        ctrl = ControllerConfig(name="lab", host="10.0.0.1")
        with (
            patch("avi.sdk.avi_api.ApiSession.get_session") as mock_get_session,
            patch.dict("os.environ", {"LAB_PASSWORD": "pw"}),
        ):
            AviConnectionManager._create_session(ctrl)

        assert mock_get_session.call_args.kwargs["verify"] is True
