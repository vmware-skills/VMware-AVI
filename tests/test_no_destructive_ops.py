"""Verify all destructive operations have double_confirm guard.

Parses source AST to confirm that every destructive function imports and
calls double_confirm before executing the operation.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "vmware_avi"

# (module_path, function_name) pairs for every destructive operation
DESTRUCTIVE_OPS = [
    (SRC_ROOT / "ops" / "vs_mgmt.py", "toggle_vs"),
    (SRC_ROOT / "ops" / "pool_mgmt.py", "toggle_pool_member"),
    (SRC_ROOT / "ops" / "ako_pod.py", "restart_ako"),
    (SRC_ROOT / "ops" / "ako_sync.py", "force_resync"),
]


def _function_calls_double_confirm(filepath: Path, func_name: str) -> bool:
    """Return True if *func_name* in *filepath* contains a call to double_confirm."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        # Walk the function body looking for a call to double_confirm
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "double_confirm":
                    return True
                if isinstance(func, ast.Attribute) and func.attr == "double_confirm":
                    return True
    return False


def _function_imports_double_confirm(filepath: Path, func_name: str) -> bool:
    """Return True if *func_name* body contains 'from vmware_avi._safety import double_confirm'."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom):
                if child.module and "safety" in child.module:
                    names = [alias.name for alias in child.names]
                    if "double_confirm" in names:
                        return True
    return False


@pytest.mark.unit
@pytest.mark.parametrize(
    "filepath,func_name",
    DESTRUCTIVE_OPS,
    ids=[f"{p.stem}::{fn}" for p, fn in DESTRUCTIVE_OPS],
)
def test_destructive_op_calls_double_confirm(filepath: Path, func_name: str) -> None:
    """Every destructive op must call double_confirm."""
    assert _function_calls_double_confirm(filepath, func_name), (
        f"{filepath.name}::{func_name} does not call double_confirm()"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "filepath,func_name",
    DESTRUCTIVE_OPS,
    ids=[f"{p.stem}::{fn}" for p, fn in DESTRUCTIVE_OPS],
)
def test_destructive_op_imports_safety(filepath: Path, func_name: str) -> None:
    """Every destructive op must import from vmware_avi._safety."""
    assert _function_imports_double_confirm(filepath, func_name), (
        f"{filepath.name}::{func_name} does not import double_confirm from _safety"
    )
