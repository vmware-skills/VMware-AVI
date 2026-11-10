"""Operations MCP registrations sharing the server runtime.

The facade owns connection, error and Policy state; this module creates no second
server or connection manager. Returned aliases preserve the original Python API.
"""

from typing import Optional


def register_tools(mcp, runtime):
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_logs(
        tail: int = 100, since: Optional[str] = None, context: Optional[str] = None
    ) -> str:
        """[READ] AKO pod logs — Ingress creation failures, sync errors, Controller
        connectivity.

        Returns raw log text, not a table. Use when get_ako_status shows the pod
        unhealthy or diff_ako_sync reports a missing Ingress. Only the running
        container's logs are returned.

        Args:
            tail: Number of log lines (default 100).
            since: Narrows the window, e.g. '30m', '1h'.
            context: K8s context (optional, uses current).
        """
        from vmware_avi.ops.ako_pod import view_ako_logs

        return runtime._capture_output(view_ako_logs, tail, since or "", context)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="high")
    def restart_ako(
        context: Optional[str] = None,
        confirm: bool = False,
    ) -> dict:
        """[WRITE] Restart the AKO pod by deleting it — its StatefulSet recreates it.

        Without confirm=True this only previews: it returns blast_radius (context,
        namespace, the pod's name, uid, phase and restarts, and the Ingresses whose
        programming pauses until the new pod is Running) and changes nothing. Show
        that to the user and get their explicit decision. Do not set confirm=True
        on your own because the user asked earlier: they have not seen what it
        changes yet. The pod deleted is the one measured (uid precondition).

        Refused with confirm=True: a pod already terminating, and a pod or Ingress
        list that cannot be read. Use when AKO is stuck or after config changes;
        brief traffic disruption is possible. Run get_ako_status afterwards, and
        get_ako_logs if the pod is not Running.

        Args:
            context: K8s context name (optional).
            confirm: False (default) returns the blast radius and changes nothing.
                True applies it.
        """
        from vmware_avi.ops import ako_gate
        from vmware_avi.ops.ako_pod import restart_ako as _ops_restart_ako
        from vmware_avi.ops.write_gate import resolve_confirm

        decision = resolve_confirm(confirm)
        effect = (
            "Deletes the AKO pod; its StatefulSet recreates it. Ingress programming pauses "
            "until the new pod is Running; brief traffic disruption is possible."
        )
        return runtime._gated(
            "restart_ako",
            lambda: ako_gate.delete_ako_pod(
                "restart_ako",
                context,
                effect,
                act=decision.act,
                apply=lambda uid: runtime._capture_output(
                    _ops_restart_ako, context, skip_prompt=True, uid=uid
                ),
            ),
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_version(context: Optional[str] = None) -> str:
        """[READ] AKO version running in a cluster, read from the pod's image tag.

        Returns the pod name and an Image/Version pair per container. Use it to
        check compatibility with the Controller, and before diff_ako_config or
        upgrade_ako_config so both target the installed chart. The tag is the only
        source, so 'latest' reports 'latest', not a number.

        Args:
            context: K8s context name (optional).
        """
        from vmware_avi.ops.ako_pod import show_ako_version

        return runtime._capture_output(show_ako_version, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_config() -> str:
        """[READ] The AKO Helm release's values — controller IP, cloud name, network
        settings, feature flags.

        Returns YAML as helm reports it. Use this first to read the live config; use
        diff_ako_config instead to see what an upgrade would change. Only values
        supplied at install time appear; chart defaults do not."""
        from vmware_avi.ops.ako_config import show_ako_config

        return runtime._capture_output(show_ako_config)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    # helm renders avicredentials.password and the avi-secret Secret into its
    # diff output. That is a free-form string, so the shared credential-key net
    # cannot see it — the declaration is the only thing that keeps it out of the
    # audit row (VMware-Policy 1.10.0). It does nothing about the *displayed* copy,
    # which in MCP mode is this tool's result: diff_ako_config redacts that.
    @runtime.vmware_tool(risk_level="low", sensitive_result=True)
    def diff_ako_config(chart_version: str = "") -> str:
        """[READ] Pending Helm value changes that have not been applied yet.

        Returns helm's diff output; empty means nothing would change. Credential
        values in it read `<redacted>` — that is this skill blanking them, not the
        configured value.
        Use this before upgrade_ako_config — it runs the same command, so the
        preview is real. Note: with chart_version empty the registry's moving latest
        is resolved, so two runs can differ with no local change; read get_ako_version
        and pass it to both.

        Args:
            chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest.
        """
        from vmware_avi.ops.ako_config import diff_ako_config as _ops_diff_ako_config

        return runtime._capture_output(_ops_diff_ako_config, chart_version=chart_version)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    # Same helm output as diff_ako_config — see the note there.
    @runtime.vmware_tool(risk_level="medium", sensitive_result=True)
    def upgrade_ako_config(
        confirm: bool = False,
        chart_version: str = "",
    ) -> dict:
        """[WRITE] Apply an AKO Helm upgrade to the avi-system release.

        Finds the avi-system release automatically and upgrades the Broadcom OCI
        chart with --reuse-values. Without confirm=True this only previews: it
        returns blast_radius (release, the chart and app version it is on, revision
        and status, the chart it would move to) plus helm_dry_run, the output of
        `helm upgrade --dry-run`, and changes nothing. Show that to the user and get
        their explicit decision. Do not set confirm=True on your own because the
        user asked earlier: they have not seen what it changes yet.

        Refused with confirm=True: a failing dry-run (the real upgrade would fail
        too), a release with another helm operation pending, and a release whose
        status cannot be read. Helm output has credential values blanked to
        `<redacted>` by this skill. Run diff_ako_config first to review the change.

        Args:
            confirm: False (default) returns the blast radius and changes nothing.
                True applies it.
            chart_version: Pin the chart, e.g. "1.11.1". Empty = registry latest,
                resolved at apply time, so it can differ from the preview.
        """
        from vmware_avi.ops import ako_config, ako_gate
        from vmware_avi.ops.write_gate import resolve_confirm

        decision = resolve_confirm(confirm)

        def apply() -> str:
            return runtime._capture_output(
                ako_config.upgrade_ako, False, chart_version=chart_version, skip_prompt=True
            )

        return runtime._gated(
            "upgrade_ako_config",
            lambda: ako_gate.upgrade(chart_version, act=decision.act, apply=apply),
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def check_ako_ingress(namespace: str, context: Optional[str] = None) -> str:
        """[READ] Validate every Ingress in one namespace: IngressClass and TLS
        secret references that would stop AKO creating a Virtual Service.

        Returns name, IngressClass, issues and OK/ISSUES per Ingress. Run
        list_ako_ingresses first for namespace names; use diagnose_ako_ingress instead
        for one named Ingress. Covers one namespace only, and TLS checks are skipped
        when its secrets cannot be listed.

        Args:
            namespace: K8s namespace to check.
            context: K8s context name (optional).
        """
        from vmware_avi.ops.ako_ingress import check_ingress_annotations

        return runtime._capture_output(check_ingress_annotations, namespace, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_ako_ingresses(context: Optional[str] = None) -> str:
        """[READ] Inventory Kubernetes Ingresses across all namespaces.

        Returns Namespace, Ingress name, Host(s) and IngressClass per Ingress. Start
        here for namespace and Ingress names, then pass them to check_ako_ingress or
        diagnose_ako_ingress. Lists the K8s side only — use diff_ako_sync for
        Ingresses with no Controller object.

        Args:
            context: K8s context name (optional).
        """
        from vmware_avi.ops.ako_ingress import show_ingress_map

        return runtime._capture_output(show_ingress_map, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def diagnose_ako_ingress(
        name: str, namespace: str = "default", context: Optional[str] = None
    ) -> str:
        """[READ] Diagnose why one Ingress has no corresponding AVI Virtual Service.

        Validates IngressClass ('avi'/'avi-lb'), TLS secrets and backend Services.
        Returns annotations, a numbered issue list and kubectl fixes. "Not
        found"/"missing" is reported only for a 404; any other error reading the
        Ingress, a TLS secret or a backend Service says it could not be read (with
        the HTTP status). Use list_ako_ingresses first to find Ingresses lacking a VS. Checks configuration
        only; when it is clean, try get_ako_logs and get_ako_sync_status.

        Args:
            name: Exact Ingress resource name.
            namespace: Namespace holding the Ingress (default 'default').
            context: kubeconfig context (optional), from list_ako_clusters.
        """
        from vmware_avi.ops.ako_ingress import diagnose_ingress

        return runtime._capture_output(diagnose_ingress, name, namespace, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_sync_status(context: Optional[str] = None) -> str:
        """[READ] Compare the number of K8s Ingresses with the number of AVI Virtual
        Services.

        Returns both counts and a match/mismatch verdict. Use this first as a cheap
        check, then diff_ako_sync for which objects differ. A count comparison only
        — in AKO shard mode many Ingresses share one VS, so a mismatch does not by
        itself mean trouble.

        Args:
            context: K8s context name (optional).
        """
        from vmware_avi.ops.ako_sync import check_sync_status

        return runtime._capture_output(check_sync_status, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def diff_ako_sync(context: Optional[str] = None) -> str:
        """[READ] List Ingresses with no matching Virtual Service or pool on the
        Controller.

        Returns Type, namespace/name and Status per suspect Ingress. Use when
        get_ako_sync_status reports a mismatch; force_ako_sync reconciles. Shard-mode
        Ingresses are matched heuristically against AKO pool names, so confirm a
        'Missing' result with list_lb_pools before acting.

        Args:
            context: K8s context name (optional).
        """
        from vmware_avi.ops.ako_sync import show_sync_diff

        return runtime._capture_output(show_sync_diff, context)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="high")
    def force_ako_sync(
        context: Optional[str] = None,
        confirm: bool = False,
    ) -> dict:
        """[WRITE] Force AKO to resync all K8s resources with the AVI Controller.

        The resync restarts the AKO pod so it rebuilds every Virtual Service, pool
        and VS-VIP from the cluster's current resources. Without confirm=True this
        only previews: it returns blast_radius (context, namespace, the pod's name,
        uid and phase, and the Ingresses it re-programs) and changes nothing. Show
        that to the user and get their explicit decision. Do not set confirm=True
        on your own because the user asked earlier: they have not seen what it
        changes yet.

        Refused with confirm=True: a pod already terminating, and a pod or Ingress
        list that cannot be read. Use when drift is detected; may cause brief
        traffic disruption. Run diff_ako_sync first to see what is out of sync,
        then get_ako_sync_status.

        Args:
            context: K8s context name (optional).
            confirm: False (default) returns the blast radius and changes nothing.
                True applies it.
        """
        from vmware_avi.ops import ako_gate
        from vmware_avi.ops.ako_sync import force_resync
        from vmware_avi.ops.write_gate import resolve_confirm

        decision = resolve_confirm(confirm)
        effect = (
            "Deletes the AKO pod to force a full resync: every Virtual Service, pool and VS-VIP "
            "is rebuilt from the cluster's resources; brief traffic disruption is possible."
        )
        return runtime._gated(
            "force_ako_sync",
            lambda: ako_gate.delete_ako_pod(
                "force_ako_sync",
                context,
                effect,
                act=decision.act,
                apply=lambda uid: runtime._capture_output(
                    force_resync, context, skip_prompt=True, uid=uid
                ),
            ),
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_ako_clusters() -> str:
        """[READ] List every Kubernetes context in the active kubeconfig and whether
        AKO is deployed there.

        Returns Context, AKO Status (pod phase, 'Not deployed' when kubectl answered
        with no AKO pods, or 'Unreachable'/'Unreadable' with kubectl's reason when it
        could not read the context — unknown, not absent) and Version per context. Fails
        when no context could be read. Requires kubectl on PATH; every context is
        probed, so unreachable clusters add latency. Start here for context names, then pass one to
        get_ako_status, get_ako_logs or diagnose_ako_ingress.
        """
        from vmware_avi.ops.ako_multi_cluster import list_clusters

        return runtime._capture_output(list_clusters)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_amko_status() -> str:
        """[READ] AMKO (AVI Multi-Cluster Kubernetes Operator) GSLB status.

        Returns raw kubectl output: the AMKO pods in avi-system, then the GSLBConfig
        YAML if one exists. 'AMKO not deployed' means kubectl answered and found no
        AMKO pods; if kubectl cannot read the cluster the call fails with its reason.
        A GSLBConfig that cannot be read is reported, not skipped. Use this only for
        multi-cluster GSLB questions — for single-cluster AKO health use
        get_ako_status instead. Always reads the current
        kubectl context; see list_ako_clusters."""
        from vmware_avi.ops.ako_multi_cluster import show_amko_status

        return runtime._capture_output(show_amko_status)

    return (
        get_ako_logs,
        restart_ako,
        get_ako_version,
        get_ako_config,
        diff_ako_config,
        upgrade_ako_config,
        check_ako_ingress,
        list_ako_ingresses,
        diagnose_ako_ingress,
        get_ako_sync_status,
        diff_ako_sync,
        force_ako_sync,
        list_ako_clusters,
        get_ako_amko_status,
    )
