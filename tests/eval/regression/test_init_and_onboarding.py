"""Regression tests for onboarding: the `vmware-avi init` wizard, the doctor
init reference (no false promise), and teaching avisdk auth/TLS errors.

The wizard must never leave a plaintext password on disk (grep-safe b64:),
must lock .env to 0600, and the stored value must round-trip to the original.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from vmware_avi import init_wizard

# ── init wizard ──────────────────────────────────────────────────────────────


@pytest.fixture
def _wizard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_dir = tmp_path / ".vmware-avi"
    monkeypatch.setattr(init_wizard, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(init_wizard, "CONFIG_FILE", cfg_dir / "config.yaml")
    monkeypatch.setattr(init_wizard, "ENV_FILE", cfg_dir / ".env")
    return cfg_dir


def _feed(monkeypatch: pytest.MonkeyPatch, answers: list[object], confirms: list[bool]) -> None:
    a = iter(answers)
    c = iter(confirms)
    monkeypatch.setattr(init_wizard.typer, "prompt", lambda *args, **kwargs: next(a))
    monkeypatch.setattr(init_wizard.typer, "confirm", lambda *args, **kwargs: next(c))


def test_init_writes_grep_safe_env(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_avi.config import _decode_secret

    _feed(
        monkeypatch,
        # name, host, username, tenant, api_version, port, password
        answers=["lab-avi", "10.1.2.3", "admin", "admin", "22.1.4", 443, "S3cr3t!pw"],
        confirms=[True],  # verify_ssl
    )
    assert init_wizard.run_init(skip_test=True) == 0

    env_text = (_wizard_env / ".env").read_text(encoding="utf-8")
    assert "LAB_AVI_PASSWORD=b64:" in env_text  # no VMWARE_ prefix for AVI
    assert "S3cr3t!pw" not in env_text  # never plaintext on disk
    assert (_wizard_env / ".env").stat().st_mode & 0o777 == 0o600
    line = next(ln for ln in env_text.splitlines() if ln.startswith("LAB_AVI_PASSWORD="))
    assert _decode_secret(line.split("=", 1)[1]) == "S3cr3t!pw"


def test_init_writes_loadable_config(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.yaml written by the wizard must load into AppConfig cleanly."""
    from vmware_avi.config import load_config

    _feed(
        monkeypatch,
        answers=["prod-avi", "avi.example.com", "admin", "admin", "30.1.1", 443, "pw"],
        confirms=[False],  # verify_ssl=No (self-signed)
    )
    assert init_wizard.run_init(skip_test=True) == 0

    cfg = load_config(_wizard_env / "config.yaml")
    ctrl = cfg.active_controller
    assert ctrl.name == "prod-avi"
    assert ctrl.host == "avi.example.com"  # FQDN accepted, not blocked
    assert ctrl.tenant == "admin"
    assert ctrl.api_version == "30.1.1"
    assert ctrl.verify_ssl is False


def test_init_accepts_fqdn_host(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An FQDN host must be accepted (connection layer resolves to IP — 踩坑 #22)."""
    _feed(
        monkeypatch,
        answers=["avi1", "controller.corp.local", "admin", "admin", "22.1.4", 443, "pw"],
        confirms=[True],
    )
    assert init_wizard.run_init(skip_test=True) == 0
    assert "host: controller.corp.local" in (_wizard_env / "config.yaml").read_text(encoding="utf-8")


# ── doctor references a real init command (no false promise) ──────────────────


def _command_names() -> set[str]:
    """Effective command names. Typer falls back to the callback function name
    (underscores → hyphens) when no explicit ``name`` is given."""
    from vmware_avi.cli import app

    names: set[str] = set()
    for c in app.registered_commands:
        name = c.name or (c.callback.__name__.replace("_", "-") if c.callback else None)
        if name:
            names.add(name)
    return names


def _init_registered() -> bool:
    return "init" in _command_names()


def test_doctor_init_reference_is_backed_by_real_command():
    from vmware_avi import doctor

    src = Path(doctor.__file__).read_text(encoding="utf-8")
    if "vmware-avi init" in src:
        assert _init_registered(), "doctor recommends init but no such command is registered"


def test_doctor_does_not_recommend_nonexistent_command():
    """Guard against doctor 'false promise': any command it tells the user to run
    must actually be registered."""
    from vmware_avi import doctor

    src = Path(doctor.__file__).read_text(encoding="utf-8")
    registered = _command_names()
    for cmd in ("init", "doctor"):
        if f"vmware-avi {cmd}" in src:
            assert cmd in registered, (
                f"doctor references 'vmware-avi {cmd}' but it is not registered"
            )


# ── avisdk auth / TLS errors teach where to fix the problem ───────────────────


def test_auth_error_is_teaching(capsys):
    from avi.sdk.avi_api import APIError

    from vmware_avi._errors import cli_errors

    @cli_errors
    def boom():
        # avisdk's own 401 login-failure message format (see authenticate_session)
        raise APIError("Failed: https://avi/login Status Code 401 msg invalid creds")

    with pytest.raises(typer.Exit):
        boom()
    out = capsys.readouterr().out
    assert ".vmware-avi/.env" in out
    assert "PASSWORD" in out
    assert "config.yaml" in out


def test_aviapierror_401_is_teaching(capsys):
    from vmware_avi._errors import cli_errors
    from vmware_avi.connection import AviApiError

    @cli_errors
    def boom():
        raise AviApiError("auth failed", status_code=401, path="login")

    with pytest.raises(typer.Exit):
        boom()
    out = capsys.readouterr().out
    assert ".vmware-avi/.env" in out


def test_tls_error_is_teaching(capsys):
    from avi.sdk.avi_api import SSLError

    from vmware_avi._errors import cli_errors

    @cli_errors
    def boom():
        raise SSLError("certificate verify failed")

    with pytest.raises(typer.Exit):
        boom()
    out = capsys.readouterr().out
    assert "verify_ssl" in out
