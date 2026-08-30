"""Safety utilities for destructive operations and output sanitization."""

from __future__ import annotations

from rich.console import Console
from vmware_policy import sanitize as _policy_sanitize

console = Console()


def sanitize(text: object, max_len: int = 500) -> str:
    """Truncate + strip control characters from AVI/K8s API text before output.

    Thin wrapper over the canonical family-wide ``vmware_policy.sanitize`` so all
    AVI ops modules share one prompt-injection defence. Non-str inputs are
    coerced to str first (API fields are occasionally None/ints).
    """
    return _policy_sanitize(text if isinstance(text, str) else str(text), max_len)


def print_external(target: Console, text: object, max_len: int = 500) -> None:
    """Print text that came from outside AVI as inert, literal output.

    Ops functions do not return data — the MCP server swaps their module
    ``console`` for a capturing one, so whatever they print becomes the tool
    result an agent reads. That makes every print of external text an output
    boundary, and it needs both defences:

    * ``sanitize`` strips control characters (Rich passes ESC straight
      through) and caps length.
    * ``markup=False`` stops Rich reading ``[...]`` as styling. Without it
      ``[bold]`` is swallowed instead of shown, and a bare ``[/]`` raises
      ``MarkupError`` and kills the command. Order matters: stripping ESC out
      of ``\\x1b[31m`` leaves ``[31m``, so the markup lever has to hold after
      sanitize has run.

    Each line is capped independently, so one very long line cannot push the
    rest past the cut-off. The caller's own line budget (e.g. ``tail``) still
    bounds how many lines arrive.

    Args:
        target: The console to write to. Passed explicitly rather than taken
            from this module, because the caller's module-level ``console`` is
            what the MCP server rebinds when capturing.
        text: Untrusted text from the Controller, Kubernetes, or a client
            request. Non-str input is coerced.
        max_len: Per-line cap handed to ``sanitize``.
    """
    body = text if isinstance(text, str) else str(text)
    target.print(
        "\n".join(sanitize(line, max_len) for line in body.split("\n")),
        markup=False,
    )


def print_command_failure(target: Console, headline: str, stderr: object) -> None:
    """Print an authored diagnosis, then the failed command's own words, inert.

    The natural way to write this is one f-string —
    ``console.print(f"[red]… Cause: {result.stderr}[/red]")`` — and it is wrong in
    two ways at once. The subprocess's text is external, so it needs
    :func:`print_external`'s defences, which an interpolation skips: a ``[bold]``
    in the error is swallowed instead of shown, and a bare ``[/]`` raises
    ``MarkupError`` and takes the command down at the exact moment it was trying
    to explain a failure. And a tool that failed rendering a template prints the
    values it was handed, so its stderr carries the same credentials as its
    stdout — hence :func:`redact_text` before it is shown.

    Two prints rather than one because only the headline is ours to style.
    """
    target.print(headline)
    body = str(stderr or "").strip()
    if body:
        print_external(target, redact_text(body), max_len=4000)


def double_confirm(action: str) -> bool:
    """Require double confirmation for destructive operations."""
    console.print(f"\n[bold red]WARNING: {action}[/bold red]")
    first = console.input("  Are you sure? (yes/no): ").strip().lower()
    if first != "yes":
        return False
    second = console.input("  Confirm again to proceed (yes/no): ").strip().lower()
    return second == "yes"


#: Value keys whose contents are credentials, in any nesting, case-insensitive.
#: `helm get values` returns the *user-supplied* values of a release, so an AKO
#: installed with `--set avicredentials.password=...` has that password sitting
#: in its output — and this family prints that output to an agent.
_SECRET_KEY_HINTS = ("password", "passwd", "secret", "token", "apikey", "api_key",
                     "credential", "privatekey", "private_key")


def redact_yaml(text: str) -> str:
    """Blank credential-shaped values in a YAML document, structurally.

    Parsed and re-emitted with the YAML parser rather than regex-substituted:
    a hand-written pattern and the parser that actually reads the file disagree
    about quoting, folded scalars and indentation, and this family has already
    been bitten by that (踩坑 #38). If the text does not parse — a partial dump,
    a helm error page — nothing is returned rather than guessing, because a
    half-redacted secret is not a redacted secret.
    """
    import yaml

    def walk(node):
        if isinstance(node, dict):
            return {
                k: ("<redacted>"
                    if isinstance(k, str)
                    and any(h in k.lower() for h in _SECRET_KEY_HINTS)
                    and not isinstance(v, (dict, list))
                    else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    if loaded is None:
        return ""
    return yaml.safe_dump(walk(loaded), default_flow_style=False, sort_keys=False)


def _is_key_shaped(token: str) -> bool:
    """True when ``token`` could be a mapping key rather than a phrase of prose.

    Keeps ``Error: failed to render password template`` — whose "key" contains
    spaces — from being mistaken for a credential assignment and mangled.
    """
    return bool(token) and all(c.isalnum() or c in "_-./" for c in token)


def _outdent(line: str) -> int:
    """Depth of ``line``, counting a diff marker as part of the indentation.

    ``+   password: |`` and ``    password: |`` describe the same nesting; the
    marker is presentation, and its width must not change what counts as a
    continuation line.
    """
    return len(line) - len(line.lstrip(" \t+-"))


def redact_text(text: str) -> str:
    """Blank credential-shaped values in line-oriented output, leaving the rest intact.

    The companion to :func:`redact_yaml` for text that is *not* a YAML document:
    ``helm diff`` output, ``helm upgrade`` output, a helm error page. Those carry
    the same ``avicredentials.password`` and the same rendered ``avi-secret``
    Secret, and this family prints them to an agent — but they will not parse, so
    ``redact_yaml`` withholds all of it and the operator loses the diff they came
    to read. Redaction has to leave the document readable or it will be turned off.

    A hand-written pattern is the wrong tool for a structured format (踩坑 #38) and
    it is not being used as one here: the input is a *line-oriented* format with no
    parser, so this works line at a time and changes nothing it does not recognise.
    Everything that is not a ``key: value`` line with a credential-shaped key comes
    back byte for byte.

    A credential written as a block scalar (``key: |``) keeps its value on the
    *following* lines, where a line-at-a-time key match would never look, so the
    continuation is dropped with it.

    This is redaction, not sanitisation: the caller still owes the output
    :func:`print_external`, which strips control characters and disarms markup.
    Redact first — ``sanitize`` truncates, and the surviving prefix of a secret is
    still secret.
    """
    out: list[str] = []
    block_depth: int | None = None
    for line in text.split("\n"):
        if block_depth is not None:
            if line.strip() and _outdent(line) > block_depth:
                continue  # still inside the redacted value
            block_depth = None

        head, sep, value = line.partition(":")
        key = head.strip().lstrip("+-").strip().strip("\"'")
        if (
            not sep
            or not value.strip()
            or not _is_key_shaped(key)
            or not any(hint in key.lower() for hint in _SECRET_KEY_HINTS)
        ):
            out.append(line)
            continue

        out.append(f"{head}: <redacted>")
        if value.strip()[0] in "|>":
            block_depth = _outdent(line)
    return "\n".join(out)
