"""Every CLI command that performs a write is wrapped by @guarded (HLD I-1, I-8).

A write CLI command must route through vmware_policy's guard() + audit_call() —
the same enforcement @vmware_tool gives the MCP surface — so ``vmware-avi vs
disable`` run through Bash is authorized and audited to ~/.vmware/audit.db exactly
like the ``vs_toggle`` MCP tool. Without @guarded a CLI write bypassed policy and
landed only in the legacy per-skill log (the gap HLD §2.1 documents).

The write set is DERIVED, never hand-listed (踩坑 #43): a tool annotated
``readOnlyHint=False`` is a write; the ops functions its body uses are the
state-changing ops; a CLI ``@command`` using one is a write command and must
carry @guarded.

AVI's MCP tools do not *call* their ops — each hands the op to
``_capture_output(op, ...)`` as an argument. A derivation that matched only
direct calls would resolve zero write ops and pass vacuously — the "label
promises more than content" shape. So the scan matches any *reference* to an
imported ops name (an ``ast.Name``/``ast.Attribute`` use), which catches the
argument-passing form; imports are ``ast.alias`` nodes, never miscounted as uses.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[3]
CLI_FILE = _REPO / "vmware_avi" / "cli.py"
SERVER_FILE = _REPO / "vmware_avi" / "mcp_server" / "server.py"
assert CLI_FILE.is_file(), f"CLI module not found at {CLI_FILE} — the scan would find nothing"
assert SERVER_FILE.is_file(), f"MCP server not found at {SERVER_FILE} — derivation would be empty"


def _write_tool_names() -> frozenset[str]:
    from vmware_avi.mcp_server.server import mcp

    return frozenset(
        t.name
        for t in asyncio.run(mcp.list_tools())
        if getattr(getattr(t, "annotations", None), "readOnlyHint", None) is False
    )


def _ops_refs(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(local name -> REAL ops function name, ops-module aliases).

    An aliased import (``from ops.mod import realname as _alias``) maps
    ``_alias -> realname`` so an aliased use resolves to the same op an
    un-aliased import names. AVI imports the real name directly; the alias
    branch is kept so this derivation stays identical to the sibling REST skills.
    """
    func_map: dict[str, str] = {}
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            parts = n.module.split(".")
            if "ops" in parts:
                if parts[-1] == "ops":
                    mods.update(a.asname or a.name for a in n.names)
                else:
                    for a in n.names:
                        func_map[a.asname or a.name] = a.name
    return func_map, mods


def _ops_used(node: ast.AST, func_map: dict[str, str], mods: set[str]) -> set[str]:
    """Real ops names referenced in ``node`` — used as ``f``/``f()`` or ``mod.f``.

    Matches any reference, not only a direct call, because AVI passes the op to
    ``_capture_output(op, ...)`` rather than calling it. Imports are ``ast.alias``
    nodes (not ``ast.Name``), so an import line is never counted as a use.
    """
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in func_map:
            out.add(func_map[n.id])
        elif (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in mods
        ):
            out.add(n.attr)
    return out


def _write_ops() -> frozenset[str]:
    targets = _write_tool_names()
    assert targets, "no [WRITE] tools (readOnlyHint=False) — the MCP surface derivation is vacuous"
    tree = ast.parse(SERVER_FILE.read_text(encoding="utf-8"))
    func_map, mods = _ops_refs(tree)
    ops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            ops |= _ops_used(node, func_map, mods)
    return frozenset(ops)


def _decorator_names(node: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, ast.Attribute):
            names.add(t.attr)
    return names


def _is_command(node: ast.FunctionDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(getattr(d, "func", None), ast.Attribute)
        and d.func.attr == "command"
        for d in node.decorator_list
    )


def _write_helpers(tree: ast.AST, func_map: dict[str, str], mods: set[str],
                   write_ops: frozenset[str]) -> dict[str, bool]:
    """Module-level non-command CLI functions that use a write op -> is it @guarded.

    ``vs enable`` / ``vs disable`` route through one guarded helper so the audit
    row carries ``enable`` (the direction). A derivation that only looked at the
    command bodies would stop seeing them as writes at all — and a write command
    routed through an *unguarded* helper would pass unnoticed.
    """
    return {
        node.name: "guarded" in _decorator_names(node)
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.FunctionDef)
        and not _is_command(node)
        and _ops_used(node, func_map, mods) & write_ops
    }


def _helpers_called(node: ast.FunctionDef, helpers: dict[str, bool]) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers
    }


def _cli_write_commands() -> tuple[list[str], list[str]]:
    """(write commands, of those the ones missing @guarded).

    A command writes if it uses a write op itself or calls a CLI helper that
    does; it is guarded if it carries @guarded itself, or if it uses no write op
    directly and every write helper it calls is @guarded.
    """
    write_ops = _write_ops()
    assert write_ops, "no write ops derived — vacuous"
    tree = ast.parse(CLI_FILE.read_text(encoding="utf-8"))
    func_map, mods = _ops_refs(tree)
    helpers = _write_helpers(tree, func_map, mods, write_ops)
    writing: list[str] = []
    unguarded: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not _is_command(node):
            continue
        direct = _ops_used(node, func_map, mods) & write_ops
        via = _helpers_called(node, helpers)
        if not (direct or via):
            continue
        writing.append(node.name)
        guarded_itself = "guarded" in _decorator_names(node)
        guarded_via = not direct and all(helpers[h] for h in via)
        if not (guarded_itself or guarded_via):
            unguarded.append(node.name)
    return writing, unguarded


def test_every_write_cli_command_is_guarded():
    writing, unguarded = _cli_write_commands()
    # vs enable/disable, pool enable/disable, ako restart/config-upgrade/sync-force.
    assert len(writing) >= 7, (
        f"only {len(writing)} write CLI commands derived ({writing}) — the "
        f"MCP→ops→CLI derivation is likely stale; a check matching almost nothing "
        f"is worse than none."
    )
    assert not unguarded, (
        f"these CLI commands use a [WRITE] ops function but are not @guarded, so "
        f"they bypass policy + audit (HLD I-1): {unguarded}"
    )


def test_named_high_blast_radius_commands_are_derived_and_guarded():
    """Pin real command names so a broad-but-wrong derivation cannot pass the floor.

    Both resolve only when the scan follows the op *reference* AVI hands to
    ``_capture_output`` — their presence proves that argument-passing path works,
    the AVI analog of AIops pinning ``deploy_ova_cmd``.
    """
    writing, _ = _cli_write_commands()
    names = set(writing)
    for must in ("vs_enable", "vs_disable", "ako_restart"):
        assert must in names, (
            f"{must} is no longer derived as a write command — the readOnlyHint→"
            f"ops→command derivation stopped resolving it"
        )


def _op_to_mcp_tools() -> dict[str, set[str]]:
    """Write ops function -> the MCP write tools whose body uses it.

    Uses ``_ops_used`` (a *reference*, not only a call) for the same reason the
    write-op derivation does: AVI tools hand the op to ``_capture_output(op,
    ...)``, so the twin is the tool that passes the same ops function the CLI
    command runs, not the helper it passes it through.
    """
    targets = _write_tool_names()
    tree = ast.parse(SERVER_FILE.read_text(encoding="utf-8"))
    func_map, mods = _ops_refs(tree)
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            for op in _ops_used(node, func_map, mods):
                out.setdefault(op, set()).add(node.name)
    return out


# Guarded CLI writes whose ops function backs more than one MCP write tool, with
# the tool each command IS. The op-level derivation cannot tell which tool the
# command mirrors, so the twin is named here rather than guessed:
# toggle_pool_member(enable=True/False) backs pool_member_enable and
# pool_member_disable, and the command's direction picks the twin. Listed
# commands are still checked — name and risk — against the twin named here, and
# the test fails if an entry stops being ambiguous, so this list cannot go stale.
_AMBIGUOUS = {
    "pool_enable": "pool_member_enable",
    "pool_disable": "pool_member_disable",
}

# Guarded CLI writes with no MCP twin at all (they keep their own name).
_CLI_ONLY: dict[str, str] = {}


def test_guarded_cli_writes_carry_their_mcp_tool_name():
    """A deny rule names a tool; it must stop the CLI twin of that tool too (HLD I-3).

    ``@guarded`` defaults the tool name to the function's ``__name__``, so
    ``ako sync-force`` was guarded as ``ako_sync_force_cmd`` while its MCP twin is
    ``ako_sync_force`` — a rule denying ``ako_sync_force`` refused the agent and
    let the same resync through the CLI, and the two surfaces wrote the one audit
    sink under two names. The twin is DERIVED: the MCP write tool that uses the
    same ops function the command uses.
    """
    from vmware_avi import cli
    from vmware_avi.mcp_server import server as srv

    op_tools = _op_to_mcp_tools()
    write_ops = frozenset(op_tools)
    tree = ast.parse(CLI_FILE.read_text(encoding="utf-8"))
    func_map, mods = _ops_refs(tree)
    checked: list[str] = []
    mismatched: list[str] = []
    stale: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        fn = getattr(cli, node.name, None)
        if not getattr(fn, "_is_guarded", False):
            continue  # unguarded writes are test_every_write_cli_command_is_guarded's finding
        ops = _ops_used(node, func_map, mods) & write_ops
        twins = set().union(*(op_tools[o] for o in ops)) if ops else set()
        if node.name in _CLI_ONLY:
            if twins:
                stale.append(f"{node.name} is listed CLI-only but maps to {sorted(twins)}")
            continue
        if node.name in _AMBIGUOUS:
            if len(twins) <= 1:
                stale.append(f"{node.name} is listed ambiguous but maps to {sorted(twins)}")
                continue
            if _AMBIGUOUS[node.name] not in twins:
                stale.append(
                    f"{node.name}: resolved to {_AMBIGUOUS[node.name]!r}, "
                    f"not one of {sorted(twins)}"
                )
                continue
            twins = {_AMBIGUOUS[node.name]}
        assert twins, (
            f"{node.name} is @guarded but uses no MCP write tool's ops — list it in "
            f"_CLI_ONLY with a reason, or the derivation is stale"
        )
        assert len(twins) == 1, (
            f"{node.name} maps to several MCP tools {sorted(twins)} — list it in "
            f"_AMBIGUOUS rather than guess"
        )
        (twin,) = twins
        checked.append(node.name)
        if fn._guarded_tool != twin:
            mismatched.append(f"{node.name}: guarded as {fn._guarded_tool!r}, MCP tool {twin!r}")
        elif fn._risk_level != getattr(srv, twin)._risk_level:
            mismatched.append(
                f"{node.name}: risk {fn._risk_level!r}, MCP tool {twin!r} "
                f"risk {getattr(srv, twin)._risk_level!r}"
            )
    assert not stale, "allowlist entries no longer hold: " + "; ".join(stale)
    assert len(checked) >= 4, f"only {checked} checked — derivation likely stale"
    assert not mismatched, (
        "these CLI writes are guarded under a different name or risk than their "
        "MCP tool, so one deny rule does not scope both surfaces — pass the MCP "
        "tool name to @guarded(...): " + "; ".join(mismatched)
    )


def test_vs_enable_and_disable_audit_rows_record_the_direction(monkeypatch):
    """`vs enable` and `vs disable` are both the MCP tool ``vs_toggle``.

    A guarded row records the guarded function's parameters. When each command
    was guarded directly, both rows read ``{"name": "web-vs"}`` — the audit
    could not say whether the Virtual Service was switched on or off. Exactly one
    row per invocation (a doubly-guarded path would write two), both under
    ``vs_toggle``, and they must differ in ``enable``.
    """
    from unittest.mock import patch

    import vmware_policy.cli_guard as cli_guard
    from typer.testing import CliRunner

    from vmware_avi import cli

    rows: list[dict] = []
    monkeypatch.setattr(cli_guard, "audit_call", lambda *a, **kw: rows.append({"args": a, **kw}))
    monkeypatch.setattr(cli, "_audit_write", lambda *a, **kw: None)

    recorded = {}
    with patch("vmware_avi.ops.vs_mgmt.toggle_vs"):
        for command in ("enable", "disable"):
            before = len(rows)
            result = CliRunner().invoke(cli.app, ["vs", command, "web-vs"])
            assert result.exit_code == 0, result.output
            new = rows[before:]
            assert len(new) == 1, f"`vs {command}` wrote {len(new)} audit rows, expected one"
            (row,) = new
            assert row["args"][1] == "vs_toggle", row["args"]
            assert row["status"] == "ok", row
            recorded[command] = row["params"]

    assert recorded["enable"] == {"name": "web-vs", "enable": True}, recorded
    assert recorded["disable"] == {"name": "web-vs", "enable": False}, recorded
