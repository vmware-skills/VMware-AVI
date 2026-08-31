"""Environment diagnostics for VMware AVI.

Checks: AVI Controller connectivity, kubeconfig validity, SDK availability.
"""

from __future__ import annotations

import importlib
import logging
import shutil
from pathlib import Path

from rich.console import Console

from vmware_avi.config import ENV_FILE, load_config, resolve_config_path
from vmware_policy.fsperms import check_secret_file

_log = logging.getLogger("vmware-avi.doctor")
console = Console()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
    msg = f"  {status}  {label}"
    if detail:
        msg += f"  [dim]({detail})[/dim]"
    console.print(msg)
    return ok


def run_doctor() -> bool:
    """Run all diagnostic checks. Returns True if all pass."""
    console.print("\n[bold]vmware-avi doctor[/bold]\n")

    # The file the tools will open, resolved exactly as they resolve it —
    # including $VMWARE_AVI_CONFIG, which this doctor used to skip. With the
    # variable set it inspected ~/.vmware-avi/config.yaml and reported on that,
    # while every ops function opened a different controller (2026-08-30).
    # Every row below names this path rather than the default, for the same
    # reason: a verdict about a file nothing reads is not a verdict.
    config_file = resolve_config_path()

    if not config_file.exists():
        console.print(
            "[yellow]No config found.[/yellow] Run [cyan]vmware-avi init[/cyan] "
            "for guided setup (writes config.yaml + .env, grep-safe password).\n"
            f"Or create {config_file} and {ENV_FILE} by hand "
            "(see config.example.yaml and .env.example).\n"
        )

    results: list[bool] = []

    # 1. Config directory
    results.append(
        _check(
            "Config directory exists",
            config_file.parent.exists(),
            str(config_file.parent),
        )
    )

    # 2. Config file
    results.append(
        _check(
            "config.yaml exists",
            config_file.exists(),
            str(config_file),
        )
    )

    # 3. .env file
    env_exists = ENV_FILE.exists()
    results.append(_check(".env file exists", env_exists, str(ENV_FILE)))

    if env_exists:

        # Three states, not two — see vmware_policy.fsperms. Windows has no
        # POSIX mode bits and `chmod 600` there exits 0 without changing
        # anything, so the old two-state check was permanently red.
        check = check_secret_file(ENV_FILE)
        results.append(
            _check(".env permissions restrict it to you", not check.is_failure, check.message)
        )

    # 4. avisdk
    try:
        avi_mod = importlib.import_module("avi.sdk.avi_api")
        results.append(_check("avisdk installed", True, getattr(avi_mod, "__version__", "ok")))
    except ImportError:
        results.append(_check("avisdk installed", False, "pip install avisdk"))

    # 5. kubernetes client
    try:
        k8s_mod = importlib.import_module("kubernetes")
        results.append(
            _check(
                "kubernetes client installed",
                True,
                getattr(k8s_mod, "__version__", "ok"),
            )
        )
    except ImportError:
        results.append(_check("kubernetes client installed", False, "pip install kubernetes"))

    # 6. kubectl binary
    kubectl = shutil.which("kubectl")
    results.append(_check("kubectl in PATH", kubectl is not None, kubectl or "not found"))

    # 7. helm binary
    helm = shutil.which("helm")
    results.append(_check("helm in PATH", helm is not None, helm or "not found"))

    # 8. kubeconfig
    if config_file.exists():
        try:
            cfg = load_config()
            kc_path = Path(cfg.ako.kubeconfig).expanduser()
            results.append(_check("kubeconfig exists", kc_path.exists(), str(kc_path)))
        except Exception as exc:
            results.append(_check("kubeconfig exists", False, str(exc)))

    # 9. Controller connectivity
    if config_file.exists():
        try:
            cfg = load_config()
            for ctrl in cfg.controllers:
                try:
                    from vmware_avi.connection import AviConnectionManager

                    mgr = AviConnectionManager(cfg)
                    mgr.connect(ctrl.name)
                    mgr.disconnect(ctrl.name)
                    results.append(
                        _check(
                            f"Controller '{ctrl.name}' reachable",
                            True,
                            ctrl.host,
                        )
                    )
                except Exception as exc:
                    results.append(
                        _check(
                            f"Controller '{ctrl.name}' reachable",
                            False,
                            str(exc)[:80],
                        )
                    )
        except Exception:
            pass

    # 10. vmware-policy
    try:
        importlib.import_module("vmware_policy")
        results.append(_check("vmware-policy installed", True))
    except ImportError:
        results.append(_check("vmware-policy installed", False, "pip install vmware-policy"))

    passed = sum(results)
    total = len(results)
    console.print(f"\n  {passed}/{total} checks passed.\n")
    if not all(results):
        console.print(
            "  Some checks failed. Run [cyan]vmware-avi init[/cyan] to (re)create "
            "config.yaml + .env, or edit them under ~/.vmware-avi/ by hand.\n"
        )
    return all(results)
