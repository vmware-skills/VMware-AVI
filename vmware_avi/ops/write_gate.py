"""Shared pieces of the MCP confirmation gate (HLD §7, revised 2026-09-16).

Every in-scope MCP write tool in this skill takes ``confirm: bool = False``:

* L2 — a bare call previews: it returns the blast radius and writes nothing.
* L1 — preview and acting response both carry ``blast_radius``: what the call
  changes, with the object's identity, ``blockers`` and ``unmeasured``.
* L3 — ``confirm=True`` is refused, with a teaching error, when a blocker is
  present or a field the blast radius depends on could not be read.

The legacy spellings ``confirmed`` and ``dry_run`` were removed in 2.4.1; a call
that still passes one is refused by the tool's argument model as an unknown name.
"""

from __future__ import annotations

from typing import Any, NamedTuple

#: Identifiers listed individually in a blast radius; counts cover the rest.
MAX_LISTED = 16

class GateRefusedError(ValueError):
    """A confirmed write refused before anything was sent (blocker or unmeasured).

    A ``ValueError`` so the MCP error sanitizer passes its teaching text
    through; it also carries the blast radius that was measured, so the caller
    sees what stood in the way.
    """

    def __init__(self, message: str, blast_radius: dict | None = None) -> None:
        super().__init__(message)
        self.blast_radius = blast_radius


class Decision(NamedTuple):
    """Whether this call acts."""

    act: bool


def resolve_confirm(confirm: Any) -> Decision:
    """Read ``confirm`` conservatively: only ``True`` itself acts."""
    return Decision(act=confirm is True)


def refusal(tool: str, subject: str, radius: dict, next_step: str) -> str | None:
    """The L3 reason ``confirm=True`` must not act on ``radius``, or None.

    Args:
        tool: Tool name, for the message.
        subject: What was measured, e.g. "Virtual Service 'web-vs'".
        radius: The blast radius just measured.
        next_step: What to do when a field could not be read.
    """
    if radius.get("blockers"):
        return f"{tool} refused for {subject}: " + " ".join(radius["blockers"])
    if radius.get("unmeasured"):
        return (
            f"{tool} refused for {subject}: could not read "
            f"{', '.join(radius['unmeasured'])}, so what the call would change is "
            f"unknown. Nothing was changed. {next_step}"
        )
    return None


def preview(radius: dict, hint: str) -> dict:
    """The L2 response: the blast radius and nothing else happened."""
    return {"action": "preview", "blast_radius": radius, "hint": hint}


PREVIEW_HINT = (
    "Nothing was changed. Show blast_radius to the user and get their explicit "
    "decision; to apply, call again with confirm=True."
)


def applied(out: str, action: str, radius: dict) -> dict:
    """Shape an executor's captured output (``_capture_output``) as the L1 response.

    The CLI executors report failure by printing and exiting; the MCP layer turns
    that into ``"Error: ..."``. That text is kept as the error, and the blast
    radius travels with it so the caller still sees what was targeted.
    """
    if out.startswith("Error:"):
        return {"error": out[len("Error:") :].strip(), "blast_radius": radius}
    return {"action": action, "blast_radius": radius, "result": out.strip()}
