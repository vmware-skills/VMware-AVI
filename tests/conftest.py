"""Shared fixtures for VMware AVI tests.

Provides mock AVI sessions, K8s clients, and config objects so that
tests run without real infrastructure.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import tempfile

from vmware_policy.audit import reset_engine

# ── session-wide sandbox: the suite must not touch the operator's real files ──
#
# At import time, not in a fixture: the per-skill audit logger binds
# Path.home() when its module is imported, and a fixture — even session-scoped
# autouse — runs after collection has already imported every test module and,
# with them, the package.
#
# OPS_HOME moves vmware_policy's shared audit.db; HOME moves the per-skill
# JSON Lines log under ~/.vmware-avi, which resolves through Path.home() and
# ignores OPS_HOME. USERPROFILE is the Windows spelling.
#
# Before this, one run of this suite appended 7 rows to the operator's real
# ~/.vmware/audit.db — which held 30,779, dominated by tool names nobody had
# invoked, including 1,400 ako_config_upgrade entries for a destructive
# operation that never happened.
REAL_HOME = pathlib.Path(os.path.expanduser("~"))
SANDBOX_HOME = pathlib.Path(tempfile.mkdtemp(prefix="vmware-avi-tests-"))
os.environ["HOME"] = str(SANDBOX_HOME)
os.environ["OPS_HOME"] = str(SANDBOX_HOME / ".vmware")
os.environ["USERPROFILE"] = str(SANDBOX_HOME)
reset_engine()
atexit.register(shutil.rmtree, SANDBOX_HOME, True)

import pathlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vmware_avi.config import AkoConfig, AppConfig, ControllerConfig


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_controller() -> ControllerConfig:
    return ControllerConfig(name="lab", host="10.0.0.1", config_username="admin")


@pytest.fixture()
def sample_ako_config() -> AkoConfig:
    return AkoConfig(kubeconfig="/tmp/fake-kubeconfig", namespace="avi-system")


@pytest.fixture()
def sample_config(
    sample_controller: ControllerConfig,
    sample_ako_config: AkoConfig,
) -> AppConfig:
    return AppConfig(
        controllers=(sample_controller,),
        default_controller="lab",
        ako=sample_ako_config,
    )


@pytest.fixture()
def config_yaml(tmp_path: Path) -> Path:
    """Write a minimal valid config.yaml and return its path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "controllers:\n"
        "  - name: lab\n"
        "    host: 10.0.0.1\n"
        "default_controller: lab\n"
        "ako:\n"
        "  namespace: avi-system\n"
    , encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Mock AVI session
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_avi_session() -> MagicMock:
    """Return a MagicMock that behaves like avi.sdk.avi_api.ApiSession."""
    session = MagicMock()
    session.get.return_value.json.return_value = {"results": []}
    session.get_object_by_name.return_value = None
    return session


@pytest.fixture()
def _patch_avi_connect(
    mock_avi_session: MagicMock,
    sample_config: AppConfig,
) -> Any:
    """Patch load_config + AviConnectionManager.connect to return the mock session.

    Ops modules bind ``load_config`` at import time via
    ``from vmware_avi.config import load_config``, so patching only
    ``vmware_avi.config.load_config`` is order-dependent: if an ops module
    was already imported (e.g. by the eval suite's import-walk test), its
    binding still points at the real function. Patch the binding in every
    already-imported ops module too.
    """
    import sys
    from contextlib import ExitStack

    with ExitStack() as stack:
        stack.enter_context(
            patch("vmware_avi.config.load_config", return_value=sample_config)
        )
        stack.enter_context(
            patch(
                "vmware_avi.connection.AviConnectionManager.connect",
                return_value=mock_avi_session,
            )
        )
        for mod_name, mod in list(sys.modules.items()):
            if (
                mod_name.startswith("vmware_avi.ops.")
                and mod is not None
                and hasattr(mod, "load_config")
            ):
                stack.enter_context(
                    patch(f"{mod_name}.load_config", return_value=sample_config)
                )
        yield


# ---------------------------------------------------------------------------
# Mock Kubernetes client
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_k8s_core_v1() -> MagicMock:
    """Return a MagicMock that behaves like kubernetes.client.CoreV1Api."""
    core = MagicMock()
    core.list_namespaced_pod.return_value.items = []
    core.read_namespaced_pod_log.return_value = "fake-log-line"
    return core


@pytest.fixture()
def _patch_k8s_connect(
    mock_k8s_core_v1: MagicMock,
    sample_config: AppConfig,
) -> Any:
    """Patch load_config + K8sConnectionManager to return mock K8s client."""
    with (
        patch("vmware_avi.config.load_config", return_value=sample_config),
        patch(
            "vmware_avi.k8s_connection.K8sConnectionManager.core_v1",
            return_value=mock_k8s_core_v1,
        ),
        patch(
            "vmware_avi.k8s_connection.K8sConnectionManager.get_client",
            return_value=MagicMock(),
        ),
    ):
        yield
