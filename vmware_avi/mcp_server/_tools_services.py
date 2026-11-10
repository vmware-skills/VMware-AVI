"""Services MCP registrations sharing the server runtime.

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
    def list_virtual_services(controller: Optional[str] = None) -> str:
        """[READ] List Virtual Services on the AVI Controller.

        Returns Name, Enabled, VIP and short UUID for every VS in one call; it
        cannot be paged or filtered, and carries no health score.
        Use this before drilling into one VS with get_virtual_service_status.

        Args:
            controller: Controller name from config (optional, uses default).
        """
        from vmware_avi.ops.vs_mgmt import list_virtual_services as _ops_list_virtual_services

        return runtime._capture_output(_ops_list_virtual_services, controller)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_virtual_service_status(name: str) -> str:
        """[READ] Detailed status for one Virtual Service: VIP, pool, health,
        connections, throughput.

        Returns one detail block, not a list. Use list_virtual_services first for the exact name —
        a name that does not match exactly fails. If the runtime read fails, the block says so
        (with the HTTP status) and health is UNKNOWN. Then get_virtual_service_analytics for
        metrics, get_virtual_service_error_logs for 5xx.

        Args:
            name: Exact Virtual Service name.
        """
        from vmware_avi.ops.vs_mgmt import show_vs_status

        return runtime._capture_output(show_vs_status, name)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(
        risk_level="high",
        undo=lambda params, result: (
            {
                "tool": "set_virtual_service_enabled",
                "params": {
                    "name": params.get("name"),
                    "enable": not params.get("enable"),
                    "confirm": True,
                },
                "skill": "avi",
                "note": (
                    "Inverse of set_virtual_service_enabled: toggle the Virtual Service "
                    "back to its prior state."
                ),
            }
            if isinstance(result, dict) and result.get("action") in ("enabled", "disabled")
            else None
        ),
    )
    def set_virtual_service_enabled(
        name: str,
        enable: bool,
        confirm: bool = False,
    ) -> dict:
        """[WRITE] Enable or disable a Virtual Service. Disabling stops all traffic to it.

        Without confirm=True this only previews: it returns blast_radius (the VS
        name and uuid, whether it is enabled now, its VIPs and oper status, and the
        pools and member counts behind it) and changes nothing. Show that to the
        user and get their explicit decision. Do not set confirm=True on your own
        because the user asked earlier: they have not seen what it changes yet.
        A VS already in the requested state returns action "noop".

        Refused with confirm=True: a VS whose uuid or pools cannot be read (the
        change would be blind). Use get_virtual_service_status first to check current state.

        Args:
            name: Exact Virtual Service name.
            enable: true to enable, false to disable.
            confirm: False (default) returns the blast radius and changes nothing.
                True applies it.
        """
        from vmware_avi.ops import lb_gate
        from vmware_avi.ops.vs_mgmt import toggle_vs
        from vmware_avi.ops.write_gate import resolve_confirm

        decision = resolve_confirm(confirm)
        return runtime._gated(
            "set_virtual_service_enabled",
            lambda: lb_gate.vs_toggle(
                name,
                enable,
                act=decision.act,
                apply=lambda: runtime._capture_output(
                    toggle_vs, name, enable=enable, skip_prompt=True
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
    def list_lb_pools(vs_filter: Optional[str] = None) -> str:
        """[READ] Discover pools on the Controller.

        Returns Name, member count, Enabled and short UUID per pool. Use this before
        list_lb_pool_members: pools are often named differently from the VS that use them.

        Args:
            vs_filter: Substring matching VS names (e.g. 'web') — returns only the
                pools those VS reference. Omit for all pools.
        """
        from vmware_avi.ops.pool_mgmt import list_pools

        return runtime._capture_output(list_pools, vs_filter)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_lb_pool_members(pool: str) -> str:
        """[READ] List the members of a pool.

        Returns Server IP, Port, Enabled and Ratio per member. Use before
        enable_lb_pool_member or disable_lb_pool_member; run list_lb_pools first for the pool
        name. Reports configured state only, not live health-monitor results.

        Args:
            pool: Exact pool name as shown by list_lb_pools — matched literally, not
                fuzzily, and an unknown name is refused rather than ignored. Pools
                are often named differently from the Virtual Services that use
                them, so do not infer it from a VS name.
        """
        from vmware_avi.ops.pool_mgmt import list_pool_members

        return runtime._capture_output(list_pool_members, pool)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="medium")
    def enable_lb_pool_member(pool: str, server: str) -> str:
        """[WRITE] Enable a pool member so it receives traffic again.

        Returns a one-line confirmation. Use list_lb_pool_members first to verify the server
        IP. The server must already belong to the pool — this adds nothing.

        Args:
            pool: Exact pool name as shown by list_lb_pools — matched literally, not
                fuzzily, and an unknown name is refused rather than ignored. Pools
                are often named differently from the Virtual Services that use
                them, so do not infer it from a VS name.
            server: Server IP address.
        """
        from vmware_avi.ops.pool_mgmt import toggle_pool_member

        return runtime._capture_output(toggle_pool_member, pool, server, enable=True)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="high")
    def disable_lb_pool_member(
        pool: str,
        server: str,
        confirm: bool = False,
    ) -> dict:
        """[WRITE] Disable a pool member with graceful drain — existing connections
        complete, no new traffic.

        Without confirm=True this only previews: it returns blast_radius (pool name
        and uuid, the member's IP/port/state, and how many members are enabled
        before and after) and changes nothing. Show that to the user and get their
        explicit decision. Do not set confirm=True on your own because the user
        asked earlier: they have not seen what it changes yet. An already disabled
        member returns action "noop".

        Refused with confirm=True: the pool's only enabled member (the pool would
        serve nothing — enable another first, or use set_virtual_service_enabled on purpose), an IP
        that matches more than one member, and a pool whose members cannot be read.
        Use for maintenance or rolling deployments; run list_lb_pool_members first for the
        server IP, enable_lb_pool_member to reverse it.

        Args:
            pool: Exact pool name as shown by list_lb_pools — matched literally, not
                fuzzily, and an unknown name is refused rather than ignored. Pools
                are often named differently from the Virtual Services that use
                them, so do not infer it from a VS name.
            server: Server IP address.
            confirm: False (default) returns the blast radius and changes nothing.
                True applies it.
        """
        from vmware_avi.ops import lb_gate
        from vmware_avi.ops.pool_mgmt import toggle_pool_member
        from vmware_avi.ops.write_gate import resolve_confirm

        decision = resolve_confirm(confirm)
        return runtime._gated(
            "disable_lb_pool_member",
            lambda: lb_gate.pool_member_disable(
                pool,
                server,
                act=decision.act,
                apply=lambda: runtime._capture_output(
                    toggle_pool_member, pool, server, enable=False, skip_prompt=True
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
    def list_ssl_certs() -> str:
        """[READ] List SSL/TLS certificates stored on the AVI Controller.

        Returns Name, Subject, Expiry and Type per certificate, in one call that
        cannot be paged or filtered. Use for inventory or a certificate's exact name
        — use check_ssl_cert_expiry instead for only the ones expiring soon.
        """
        from vmware_avi.ops.ssl_mgmt import list_certificates

        return runtime._capture_output(list_certificates)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def check_ssl_cert_expiry(days: int = 30) -> str:
        """[READ] Check which SSL certificates expire within N days (default 30).

        Returns name, expiry date and days remaining, soonest first. Use this
        instead of list_ssl_certs when you only want certificates near expiry. Expired
        certs are included, with negative days remaining. Certificates whose expiry
        could not be read are counted and named separately — they are unknown, not
        "not expiring".

        Args:
            days: Report certs expiring within this many days (default 30).
        """
        from vmware_avi.ops.ssl_mgmt import check_expiry

        return runtime._capture_output(check_expiry, days)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_virtual_service_analytics(vs_name: str) -> str:
        """[READ] Performance metrics for one Virtual Service over the last hour.

        Returns L4 (bandwidth, connections) and L7 (latency, % errors, responses)
        averages over a fixed window that cannot be changed (12 samples, 5 min
        apart). Empty output means no traffic, not an error. Use when get_virtual_service_status
        shows degraded health; get_virtual_service_error_logs gives per-request detail.

        Args:
            vs_name: Exact Virtual Service name, case-sensitive, from list_virtual_services.
        """
        from vmware_avi.ops.analytics import show_analytics

        return runtime._capture_output(show_analytics, vs_name)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_virtual_service_error_logs(vs_name: str, since: str = "1h") -> str:
        """[READ] Recent request error logs for one Virtual Service.

        Returns up to 50 lines — timestamp, HTTP status, URI path, client IP — for
        status 400 and above. Use this instead of get_virtual_service_analytics for per-request
        detail. An empty result may mean no errors, or capture disabled on the VS.

        Args:
            vs_name: Exact Virtual Service name, from list_virtual_services.
            since: Window — seconds or '30m', '1h', '2d' (default '1h').
        """
        from vmware_avi.ops.analytics import show_error_logs

        return runtime._capture_output(show_error_logs, vs_name, since)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def list_service_engines() -> str:
        """[READ] List Service Engines (AVI data-plane VMs) on the Controller.

        Returns Name, management IP, status (e.g. OPER_UP) and SE Group per SE, in
        one call that cannot be paged or filtered. Use to inventory capacity or find
        an SE's name and IP — use get_service_engine_health instead for degraded VS health.
        """
        from vmware_avi.ops.se_mgmt import list_service_engines as _ops_list_service_engines

        return runtime._capture_output(_ops_list_service_engines)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_service_engine_health() -> str:
        """[READ] Health of every Service Engine — operational status and VS counts.

        Returns name, operational state and the number of VSes placed on each SE.
        Use when VS health degrades to check if the issue is at the SE level;
        list_service_engines gives the management IP and SE Group,
        get_virtual_service_status the affected VS.
        An SE hosting no VS reports 0, not an error.
        """
        from vmware_avi.ops.se_mgmt import check_se_health

        return runtime._capture_output(check_se_health)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    def get_ako_status(context: Optional[str] = None) -> str:
        """[READ] Check AKO (AVI Kubernetes Operator) pod status in Kubernetes.

        Returns pod name, phase, ready flag, restart count and namespace. First step
        for Ingress or LoadBalancer issues in Tanzu/K8s; follow with get_ako_logs when
        it is not Running. Looks in one context's AKO namespace only — run
        list_ako_clusters if not found.

        Args:
            context: K8s context name (optional, uses current context).
        """
        from vmware_avi.ops.ako_pod import check_ako_status

        return runtime._capture_output(check_ako_status, context)

    return (
        list_virtual_services,
        get_virtual_service_status,
        set_virtual_service_enabled,
        list_lb_pools,
        list_lb_pool_members,
        enable_lb_pool_member,
        disable_lb_pool_member,
        list_ssl_certs,
        check_ssl_cert_expiry,
        get_virtual_service_analytics,
        get_virtual_service_error_logs,
        list_service_engines,
        get_service_engine_health,
        get_ako_status,
    )
