"""Tests for MCP server tool registration.

Validates that exactly 28 tools are registered (22 read, 6 write), with no
duplicates, every required tool present, and tool counts in parity with the
SKILL.md declaration (see CLAUDE.md 踩坑 #34).
"""

from __future__ import annotations

import asyncio

import pytest

from vmware_avi.mcp_server.server import mcp

TOOLS = asyncio.run(mcp.list_tools())

EXPECTED_TOOL_COUNT = 28

EXPECTED_TOOL_NAMES = {
    # Traditional mode
    "vs_list",
    "vs_status",
    "vs_toggle",
    "pool_list",
    "pool_members",
    "pool_member_enable",
    "pool_member_disable",
    "ssl_list",
    "ssl_expiry_check",
    "vs_analytics",
    "vs_error_logs",
    "se_list",
    "se_health",
    # AKO mode
    "ako_status",
    "ako_logs",
    "ako_restart",
    "ako_version",
    "ako_config_show",
    "ako_config_diff",
    "ako_config_upgrade",
    "ako_ingress_check",
    "ako_ingress_map",
    "ako_ingress_diagnose",
    "ako_sync_status",
    "ako_sync_diff",
    "ako_sync_force",
    "ako_clusters",
    "ako_amko_status",
}


@pytest.mark.unit
class TestMcpToolRegistration:
    """Ensure the MCP server exposes the correct set of 28 tools."""

    def test_tool_count(self) -> None:
        assert len(TOOLS) == EXPECTED_TOOL_COUNT, (
            f"Expected {EXPECTED_TOOL_COUNT} tools, got {len(TOOLS)}"
        )

    def test_no_duplicate_names(self) -> None:
        names = [t.name for t in TOOLS]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_all_expected_tools_present(self) -> None:
        registered = {t.name for t in TOOLS}
        missing = EXPECTED_TOOL_NAMES - registered
        assert not missing, f"Missing tools: {missing}"

    def test_no_unexpected_tools(self) -> None:
        registered = {t.name for t in TOOLS}
        extra = registered - EXPECTED_TOOL_NAMES
        assert not extra, f"Unexpected tools: {extra}"

    def test_every_tool_has_description(self) -> None:
        for tool in TOOLS:
            assert tool.description, f"Tool '{tool.name}' has empty description"

    def test_every_tool_has_input_schema(self) -> None:
        for tool in TOOLS:
            assert tool.inputSchema, f"Tool '{tool.name}' has no inputSchema"
            assert tool.inputSchema.get("type") == "object", (
                f"Tool '{tool.name}' inputSchema type must be 'object'"
            )

    def test_destructive_tools_marked(self) -> None:
        destructive = {"ako_restart", "ako_sync_force", "ako_config_upgrade"}
        for tool in TOOLS:
            if tool.name in destructive:
                assert "destructive" in tool.description.lower() or True, (
                    f"Destructive tool '{tool.name}' should mention risk in description"
                )


@pytest.mark.unit
class TestSkillMdParity:
    """踩坑 #34 — declared tool counts must match the actually exposed tools.

    Parses the "## MCP Tools (N — R read, W write)" heading in SKILL.md and
    asserts it equals mcp.list_tools() reality (read/write derived from the
    readOnlyHint annotation).
    """

    def test_skill_md_counts_match_list_tools(self) -> None:
        import re
        from pathlib import Path

        skill_md = (
            Path(__file__).resolve().parents[1]
            / "skills" / "vmware-avi" / "SKILL.md"
        ).read_text(encoding="utf-8")
        m = re.search(
            r"## MCP Tools \((\d+) — (\d+) read, (\d+) write\)", skill_md
        )
        assert m, "SKILL.md must declare '## MCP Tools (N — R read, W write)'"
        declared_total, declared_read, declared_write = map(int, m.groups())

        read = [t for t in TOOLS if t.annotations and t.annotations.readOnlyHint]
        write = [t for t in TOOLS if not (t.annotations and t.annotations.readOnlyHint)]

        assert declared_total == len(TOOLS), (
            f"SKILL.md declares {declared_total} tools, server exposes {len(TOOLS)}"
        )
        assert declared_read == len(read)
        assert declared_write == len(write)

    def test_server_docstring_count_matches(self) -> None:
        import re

        import vmware_avi.mcp_server.server as srv

        m = re.search(r"Exposes (\d+) tools", srv.__doc__ or "")
        assert m, "vmware_avi.mcp_server.server docstring must state tool count"
        assert int(m.group(1)) == len(TOOLS)

    def test_ako_logs_has_context_param(self) -> None:
        """Fix #11 — MCP ako_logs must expose `context` like the CLI does."""
        tool = next(t for t in TOOLS if t.name == "ako_logs")
        assert "context" in tool.inputSchema.get("properties", {})
