"""Configuration management for VMware AVI.

Loads AVI Controller targets and AKO settings from YAML config + environment variables.
Passwords are NEVER stored in config files — always via environment variables.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values, load_dotenv, set_key

CONFIG_DIR = Path.home() / ".vmware-avi"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"

_log = logging.getLogger("vmware-avi.config")

_PW_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*_PASSWORD")


def _is_b64_token(value: str) -> tuple[bool, str]:
    """Return ``(True, decoded)`` if ``value`` is a valid ``b64:`` token, else ``(False, "")``.

    Recognises already-encoded values (for idempotency) and decodes on read. A
    value that merely *starts with* ``b64:`` but is not valid base64 (e.g. a real
    password ``b64:hunter2``) is NOT a token — it is treated as plaintext, so such
    a password still round-trips correctly instead of being corrupted.
    """
    if not value.startswith("b64:"):
        return (False, "")
    try:
        return (True, base64.b64decode(value[4:], validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return (False, "")


def _decode_secret(value: str) -> str:
    """Decode a ``b64:`` token; any other value passes through unchanged.

    Obfuscation to defeat casual grep — NOT encryption.
    """
    ok, decoded = _is_b64_token(value)
    return decoded if ok else value


def _autoencode_env_file(env_file: Path) -> None:
    """Rewrite plaintext ``*_PASSWORD`` values in .env to grep-safe ``b64:`` form.

    Values are read and written through python-dotenv's own parser/serializer
    (``dotenv_values`` + ``set_key``), so the stored value is exactly what
    ``load_dotenv`` would return — quoting, inline comments, and trailing
    whitespace are handled identically and the secret never drifts from the
    configured one. Idempotent (already-``b64:`` tokens are skipped); only
    ``*_PASSWORD`` keys are touched. Obfuscation, not encryption.
    """
    if not env_file.exists():
        return
    try:
        parsed = dotenv_values(env_file)
    except OSError:
        return

    changed = False
    for key, value in parsed.items():
        if not value or not _PW_KEY_RE.fullmatch(key) or _is_b64_token(value)[0]:
            continue
        encoded = "b64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
        try:
            set_key(str(env_file), key, encoded, quote_mode="never")
            changed = True
        except OSError as exc:
            _log.warning("Could not auto-encode %s in %s: %s", key, env_file, exc)

    if not changed:
        return
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    _log.warning(
        "Auto-encoded plaintext password(s) in %s to b64: (grep-safe; "
        "obfuscation, not encryption).",
        env_file,
    )


# Auto-encode any plaintext passwords in .env, then load it into the environment
_autoencode_env_file(ENV_FILE)
load_dotenv(ENV_FILE)


def _check_env_permissions() -> None:
    """Warn if .env file has permissions wider than owner-only (600)."""
    if not ENV_FILE.exists():
        return
    try:
        mode = ENV_FILE.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            _log.warning(
                "Security warning: %s has permissions %s (should be 600). Run: chmod 600 %s",
                ENV_FILE,
                oct(stat.S_IMODE(mode)),
                ENV_FILE,
            )
    except OSError:
        pass


_check_env_permissions()


class ConfigError(OSError):
    """A configuration problem the operator can fix, safe to show an agent.

    Subclasses ``OSError`` so the CLI paths that already catch ``OSError`` keep
    working. The point of the narrow type is the MCP path: ``_safe_error``
    passes this through verbatim, and passing through bare ``OSError`` also
    passed through TLS, DNS and socket errors carrying hostnames and URLs.
    """


@dataclass(frozen=True)
class ControllerConfig:
    """An AVI Controller connection target."""

    name: str
    host: str
    config_username: str = "admin"
    """Username as written in config.yaml. Read :attr:`username` instead — the
    env var overrides this, and the override is what actually gets used."""
    api_version: str = "22.1.4"
    tenant: str = "admin"
    port: int = 443
    verify_ssl: bool = True
    environment: str = ""
    """Which environment this controller is, e.g. production / staging / lab.

    An optional label. A ``deny`` rule may scope itself to an environment
    (for example, refusing a tool only where ``environment: production``); a
    target that declares none is simply not matched by such a rule and is
    never refused for lacking a label. See :mod:`vmware_policy.environment`.
    """

    @property
    def username(self) -> str:
        """Username for this controller, env var winning over config.yaml.

        Resolved on every access, exactly like :attr:`password`. Reading it
        once at load time would split the pair the override exists to keep
        whole: a secret sidecar that rotates both halves mid-process would
        move the password and leave the username behind, and the login would
        use an account/password combination that was never issued together.
        """
        return os.environ.get(
            f"{self.name.upper().replace('-', '_')}_USERNAME",
            self.config_username,
        )

    @property
    def password(self) -> str:
        env_key = f"{self.name.upper().replace('-', '_')}_PASSWORD"
        pw = os.environ.get(env_key, "")
        if not pw:
            raise ConfigError(
                "Password not found. Add it to ~/.vmware-avi/.env (chmod 600), or "
                f"export the environment variable {env_key}"
            )
        return _decode_secret(pw)


@dataclass(frozen=True)
class AkoConfig:
    """AKO (Avi Kubernetes Operator) connection settings."""

    kubeconfig: str = str(Path.home() / ".kube" / "config")
    default_context: str = ""
    namespace: str = "avi-system"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config."""

    controllers: tuple[ControllerConfig, ...] = ()
    default_controller: str = ""
    ako: AkoConfig = field(default_factory=AkoConfig)

    def get_controller(self, name: str) -> ControllerConfig:
        for c in self.controllers:
            if c.name == name:
                return c
        available = ", ".join(c.name for c in self.controllers)
        raise KeyError(
            f"Controller '{name}' not found. Run 'vmware-avi config' to see the "
            "configured controllers, or add this one under 'controllers:' in "
            f"~/.vmware-avi/config.yaml. Available: {available}"
        )

    def environment_for(self, name: str | None) -> str:
        """Return the environment declared by ``name``, or by the default controller.

        An empty name means "the caller omitted --controller", which resolves to
        ``default_controller`` — the same controller the connection layer would
        use, so policy and connection never disagree about which host is in
        play. Returns "" when the controller is unknown or declares nothing.
        """
        try:
            controller = self.get_controller(name) if name else self.active_controller
        except (KeyError, ValueError):
            return ""
        return controller.environment

    @property
    def active_controller(self) -> ControllerConfig:
        if self.default_controller:
            return self.get_controller(self.default_controller)
        if not self.controllers:
            raise ValueError(
                "No controllers configured — at least one entry is expected under "
                "'controllers:' in ~/.vmware-avi/config.yaml. Run 'vmware-avi init' "
                "to create one interactively."
            )
        return self.controllers[0]


def resolve_config_path(config_path: Path | None = None) -> Path:
    """Which config file this skill will read: explicit arg, env var, default.

    The single place that precedence lives. Before 2026-08-30 it was written
    out three times and no two of them agreed: this function's job was done
    inline in ``load_config``, which ignored ``VMWARE_AVI_CONFIG`` entirely —
    and every tool reaches its controller through an ``ops`` function calling
    ``load_config()``, so no tool honoured the variable; the MCP server's
    policy environment resolver went through ``mtime_cached_loader``, which
    does read it; and the doctor checked ``CONFIG_FILE`` and ``CONFIG_DIR``. So
    the policy engine scoped its rules from one file while the operation it was
    gating ran against a controller named in another, and the doctor reported
    on a third possibility. The variable is this skill's advertised
    ``primaryEnv``, so the CLI honouring it is the documented behaviour —
    ignoring it was the bug. Copies of a rule do not disagree loudly; they
    disagree slowly (形态 #6).
    """
    if config_path is not None:
        return config_path
    env_override = os.environ.get("VMWARE_AVI_CONFIG")
    return Path(env_override) if env_override else CONFIG_FILE


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config from YAML file, with env var overrides for passwords."""
    path = resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            "Config file not found. Run 'vmware-avi init' to create one, or copy "
            f"config.example.yaml to {path} and edit it. Looked in: {path}"
        )

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if isinstance(raw, dict) and "read_only" in raw:
        _log.warning(
            "'read_only' in config is no longer honored (the skill-level read-only "
            "switch was removed in v1.8.7). To run this agent read-only, point it at "
            "a read-only vCenter/NSX service account (RBAC) — enforced at the "
            "platform. Remove the 'read_only' key to silence this warning."
        )

    controllers = tuple(
        ControllerConfig(
            name=c["name"],
            host=c["host"],
            config_username=c.get("username", "admin"),
            api_version=c.get("api_version", "22.1.4"),
            tenant=c.get("tenant", "admin"),
            port=c.get("port", 443),
            verify_ssl=c.get("verify_ssl", True),
            environment=str(c.get("environment", "") or "").strip(),
        )
        for c in raw.get("controllers", [])
    )

    ako_raw = raw.get("ako", {})
    ako = AkoConfig(
        kubeconfig=ako_raw.get("kubeconfig", str(Path.home() / ".kube" / "config")),
        default_context=ako_raw.get("default_context", ""),
        namespace=ako_raw.get("namespace", "avi-system"),
    )

    return AppConfig(
        controllers=controllers,
        default_controller=raw.get("default_controller", ""),
        ako=ako,
    )
