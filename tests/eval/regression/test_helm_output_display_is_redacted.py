"""Redacting the stored copy of a credential does not help if it is still displayed.

Real-hardware review finding, 2026-08-30.

The previous release declared ``ako_config_diff`` and ``ako_config_upgrade``
``sensitive_result=True``, which keeps their output out of ``~/.vmware/audit.db``.
Both then called ``print_external(...)`` on the raw helm output, and in MCP mode
that print *is* the tool result: the whole point of ``_capture_output`` is that
what an ops function prints becomes what the agent reads. So the credential was
kept out of the audit row and handed to the agent and to the operator's terminal
in the same call.

That is worse than not claiming redaction, because the release notes now say the
credential is protected. Half a boundary is not a boundary.

The exposure is real for both commands. ``helm diff upgrade --reuse-values``
renders the release's own values, so ``avicredentials.password`` appears in the
diff, and it renders the ``avi-secret`` Secret, whose ``data.password`` is the
same credential base64-encoded. ``helm upgrade`` prints the same material.

``redact_yaml`` cannot be used here: a helm diff is not a YAML document — it has
``+``/``-`` markers and section headers — so the parser returns nothing and the
operator, who needs to read the diff, gets a blank. This needs a line-oriented
redactor: blank the value on a credential-shaped key, leave every other line
exactly as it was. The controls below pin that half: a diff with no credential in
it must come out byte-identical, or the fix has cost the tool its purpose.

Assertions are on captured stdout, not on a return value. The defect is
precisely that the two differ — the ops functions return ``None`` and the
credential travels by print.

Also covered here are the other stdout writers in this package that carry
external command output: the helm ``stderr`` paths in the same module, which
interpolated raw text into a Rich markup string, and ``show_amko_status``'s
``kubectl get gslbconfig -o yaml``, which was printed with a bare
``console.print`` — no redaction, no ``sanitize``, no ``markup=False``.
"""

from __future__ import annotations

import subprocess

import pytest

from vmware_avi._safety import redact_text

PASSWORD = "hunter2"
BASE64_PASSWORD = "aHVudGVyMg=="

# Shaped like real `helm diff upgrade` output: section headers, +/- markers, and
# both places the AKO credential shows up — the release values and the rendered
# avi-secret Secret.
HELM_DIFF = f"""\
avi-system, avi-secret, Secret (v1) has changed:
  # Source: ako/templates/secret.yaml
  apiVersion: v1
  kind: Secret
  data:
-   username: YWRtaW4=
-   password: b2xkcGFzcw==
+   username: YWRtaW4=
+   password: {BASE64_PASSWORD}
avi-system, ako, StatefulSet (apps/v1) has changed:
  spec:
    template:
      spec:
        containers:
-       - image: projects.packages.broadcom.com/ako/ako:1.11.1
+       - image: projects.packages.broadcom.com/ako/ako:1.12.1
          env:
          - name: CTRL_IPADDRESS
            value: avi.internal
          avicredentials:
            username: admin
-           password: oldpass
+           password: {PASSWORD}
"""

CLEAN_DIFF = """\
avi-system, ako, StatefulSet (apps/v1) has changed:
  spec:
    template:
      spec:
        containers:
-       - image: projects.packages.broadcom.com/ako/ako:1.11.1
+       - image: projects.packages.broadcom.com/ako/ako:1.12.1
          env:
          - name: CTRL_IPADDRESS
            value: avi.internal
          - name: SERVICE_TYPE
            value: ClusterIP
"""

HELM_UPGRADE = f"""\
Release "ako-1699999999" has been upgraded. Happy Helming!
NAME: ako-1699999999
LAST DEPLOYED: Sat Aug 30 12:00:00 2026
NAMESPACE: avi-system
STATUS: deployed
REVISION: 4
USER-SUPPLIED VALUES:
avicredentials:
  username: admin
  password: {PASSWORD}
ControllerSettings:
  controllerHost: avi.internal
"""


def _run_returning(stdout: str, *, returncode: int = 0, stderr: str = ""):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], returncode, stdout, stderr)

    return fake_run


# --- the redactor itself -----------------------------------------------------


def test_a_credential_line_loses_its_value():
    assert PASSWORD not in redact_text(f"          password: {PASSWORD}\n")


def test_a_diff_marked_credential_line_loses_its_value():
    """The marker sits before the key, so a naive key match misses every diff line."""
    assert PASSWORD not in redact_text(f"+   password: {PASSWORD}\n")
    assert BASE64_PASSWORD not in redact_text(f"-   password: {BASE64_PASSWORD}\n")


@pytest.mark.parametrize(
    "key", ["password", "passwd", "apiToken", "client_secret", "PRIVATE_KEY", "apiKey", "authtoken"]
)
def test_credential_shaped_keys_are_caught_in_any_case(key):
    assert "s3cret" not in redact_text(f"+   {key}: s3cret\n")


def test_a_multi_line_credential_value_does_not_survive_on_its_continuation_lines():
    """A block scalar puts the secret on the *following* lines, where a
    line-at-a-time key match never looks."""
    text = (
        "  privateKey: |\n"
        "    -----BEGIN KEY-----\n"
        "    s3cretmaterial\n"
        "    -----END KEY-----\n"
        "  next: kept\n"
    )

    out = redact_text(text)

    assert "s3cretmaterial" not in out
    assert "next: kept" in out, "the redaction swallowed the rest of the document"


def test_text_with_no_credential_is_returned_unchanged():
    """The control that decides whether this is usable: an operator diffing an
    AKO config has to get the diff."""
    assert redact_text(CLEAN_DIFF) == CLEAN_DIFF


def test_prose_that_merely_mentions_a_password_is_not_mangled():
    """The sentence before the colon has to be shaped like a key, not like English.

    The obvious version of this test — a line whose *value* mentions a password —
    never reaches the shape check at all, because the key is "Error" and no hint
    matches it. It passes whether the check exists or not. These lines put the
    hint word on the left of the colon, where the check is the only thing
    standing between helm's NOTES and a mangled instruction.
    """
    for line in (
        "  Set the avicredentials.password with: helm upgrade --set avicredentials.password=…\n",
        "The AVI password was read from: values.yaml\n",
        "Error: failed to render password template for release ako-1\n",
    ):
        assert redact_text(line) == line, (
            f"prose was redacted as if it were an assignment: {line!r}"
        )


def test_a_key_with_no_value_is_left_alone():
    assert redact_text("  password:\n") == "  password:\n"


# --- the display paths -------------------------------------------------------


def test_diff_ako_config_does_not_print_the_credential(monkeypatch, capsys):
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(subprocess, "run", _run_returning(HELM_DIFF))

    ako_config.diff_ako_config("avi-system")

    out = capsys.readouterr().out
    assert PASSWORD not in out, "the Controller password reached the agent's context"
    assert BASE64_PASSWORD not in out, "the avi-secret Secret's base64 password reached it too"
    assert "StatefulSet" in out, "redaction cost the operator the diff they came for"
    assert "ako:1.12.1" in out


def test_upgrade_ako_does_not_print_the_credential(monkeypatch, capsys):
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(subprocess, "run", _run_returning(HELM_UPGRADE))

    ako_config.upgrade_ako(dry_run=False, namespace="avi-system", skip_prompt=True)

    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "has been upgraded" in out, "the operator still needs helm's answer"
    assert "avi.internal" in out


def test_a_clean_diff_still_prints_in_full(monkeypatch, capsys):
    """The control, end to end rather than on the helper alone."""
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(subprocess, "run", _run_returning(CLEAN_DIFF))

    ako_config.diff_ako_config("avi-system")

    out = capsys.readouterr().out
    for kept in ("StatefulSet", "CTRL_IPADDRESS", "avi.internal", "SERVICE_TYPE", "ClusterIP"):
        assert kept in out, f"{kept!r} was lost from a diff that has no credential in it"


def test_no_pending_changes_still_says_so(monkeypatch, capsys):
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(subprocess, "run", _run_returning(""))

    ako_config.diff_ako_config("avi-system")

    assert "No pending changes" in capsys.readouterr().out


# --- the failure paths, which print helm's own error text --------------------


def test_a_failed_diff_does_not_print_the_credential_in_helms_error(monkeypatch, capsys):
    """helm renders the values it was given when a template fails, so stderr is
    the same exposure as stdout — and it went out through a Rich markup string,
    where `[...]` in the error is parsed rather than shown."""
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_returning(
            "", returncode=1, stderr=f"Error: template failed\n  password: {PASSWORD}\n"
        ),
    )

    with pytest.raises(SystemExit):
        ako_config.diff_ako_config("avi-system")

    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "template failed" in out, "the operator still needs to know why it failed"


def test_a_failed_upgrade_does_not_print_the_credential_in_helms_error(monkeypatch, capsys):
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_returning("", returncode=1, stderr=f"Error: upgrade failed\n  password: {PASSWORD}\n"),
    )

    with pytest.raises(SystemExit):
        ako_config.upgrade_ako(dry_run=False, namespace="avi-system", skip_prompt=True)

    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "upgrade failed" in out


def test_a_failed_helm_list_does_not_print_its_error_as_markup(monkeypatch, capsys):
    """`[...]` in helm's error is swallowed by Rich rather than shown, and a bare
    `[/]` raises MarkupError and takes the command down with it."""
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(
        subprocess, "run", _run_returning("", returncode=1, stderr="Error: context [/] not found")
    )

    with pytest.raises(SystemExit):
        ako_config._find_ako_release("avi-system")

    out = capsys.readouterr().out
    assert "[/]" in out, "the error text was parsed as markup instead of shown"


def test_a_failed_get_values_does_not_print_the_credential_in_helms_error(monkeypatch, capsys):
    from vmware_avi.ops import ako_config

    monkeypatch.setattr(ako_config, "_find_ako_release", lambda ns: "ako-1699999999")
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_returning(
            "", returncode=1, stderr=f"Error: release not found\n  password: {PASSWORD}\n"
        ),
    )

    with pytest.raises(SystemExit):
        ako_config.show_ako_config("avi-system")

    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "release not found" in out


# --- the other raw stdout writer in this package -----------------------------


def test_a_kubectl_failure_does_not_print_its_error_as_markup(monkeypatch, capsys):
    """The same shape as the four helm sites, in the other module that shells out.

    Fixing one instance of a pattern and leaving its siblings is how this family
    keeps re-finding the same defect (形态 #7), so the sweep covers every writer
    of external command output in the package, not only the two named in the
    finding.
    """
    from vmware_avi.ops import ako_multi_cluster

    monkeypatch.setattr(
        subprocess,
        "run",
        _run_returning("", returncode=1, stderr="error: context [/] is not readable"),
    )

    with pytest.raises(SystemExit):
        ako_multi_cluster.list_clusters()

    out = capsys.readouterr().out
    assert "[/]" in out, "the error text was parsed as markup instead of shown"
    assert "kubectl is on PATH" in out, "the authored diagnosis was lost"


def test_gslb_config_is_not_printed_raw(monkeypatch, capsys):
    """`kubectl get gslbconfig -o yaml` went out through a bare console.print:
    no redaction, no sanitize, no markup=False."""
    from vmware_avi.ops import ako_multi_cluster

    gslb = (
        "apiVersion: amko.vmware.com/v1alpha1\n"
        "kind: GSLBConfig\n"
        "spec:\n"
        "  memberClusters:\n"
        "  - clusterContext: cluster-1\n"
        f"  gslbLeaderCredentials:\n    password: {PASSWORD}\n"
        "  refreshInterval: 300\n"
    )

    def fake_run(cmd, *args, **kwargs):
        if "gslbconfig" in cmd:
            return subprocess.CompletedProcess(cmd, 0, gslb, "")
        return subprocess.CompletedProcess(cmd, 0, "ako-0  Running\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ako_multi_cluster.show_amko_status()

    out = capsys.readouterr().out
    assert PASSWORD not in out, "the GSLB leader credential reached the agent's context"
    assert "GSLBConfig" in out, "the operator still needs to see the config"
    assert "refreshInterval" in out
