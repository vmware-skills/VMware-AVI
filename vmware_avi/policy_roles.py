"""What vmware-policy needs to apply roles to this skill (HLD §18 D-21/D-22).

Policy cannot see which tools escape a role or how a missing target resolves; this
module declares them. Imported by ``policy_environment``, so the MCP server and
the CLI both register it. With a vmware-policy that has no roles module (< 1.24)
nothing is declared.
"""

from __future__ import annotations

from vmware_policy import skill_name

from vmware_avi.policy_environment import _cached_config

#: AKO tools act on a Kubernetes context, not on an AVI controller target, so a
#: controller-limited role cannot use them (scope "all"). Every other tool without a
#: ``controller`` argument acts on the default controller (``untagged_scope="one"``).
AKO_TOOLS = frozenset(
    {
        "check_ako_ingress",
        "diagnose_ako_ingress",
        "diff_ako_config",
        "diff_ako_sync",
        "force_ako_sync",
        "get_ako_amko_status",
        "get_ako_config",
        "get_ako_logs",
        "get_ako_status",
        "get_ako_sync_status",
        "get_ako_version",
        "list_ako_clusters",
        "list_ako_ingresses",
        "restart_ako",
        "upgrade_ako_config",
    }
)

#: Tools that give their holder more than a role. The AKO config tools return Helm
#: values that can hold the controller password. ``get_ako_config`` is not one: it
#: redacts credential values and withholds output it cannot redact. Host-path tools
#: are added by policy while a file root is set.
ESCAPES = frozenset(
    {
        "diff_ako_config",
        "upgrade_ako_config",
    }
)


def _target_name(target: str | None) -> str:
    """The target the connection layer would use for ``target`` (or the default)."""
    config = _cached_config()
    return (config.get_controller(target) if target else config.active_controller).name


try:
    from vmware_policy.roles import declare_role_metadata
except ImportError:  # vmware-policy without roles: nothing to declare
    declare_role_metadata = None

if declare_role_metadata is not None:
    declare_role_metadata(
        skill_name(__name__),
        escapes=ESCAPES,
        resolver=_target_name,
        target_param="controller",
        untagged_scope="one",
        target_scope={name: "all" for name in AKO_TOOLS},
    )
