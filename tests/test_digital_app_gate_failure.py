import ast
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _load_gate():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_digital_app_gate_failure"
    )
    namespace = {
        "Path": Path,
        "json": json,
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(main_path), "exec"), namespace)
    return namespace["_digital_app_gate_failure"]


def test_arch2rtl_final_tool_summary_overrides_later_partial_status(tmp_path):
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "rtl_agent_summary.txt").write_text(
        "Icarus compile: pass\nVerilator lint: pass\nIssue count: 0\n",
        encoding="utf-8",
    )

    gate = _load_gate()
    assert gate("arch2rtl", {"status": "Spec2RTL conformance partial"}, str(tmp_path)) is None


def test_arch2rtl_final_tool_failure_blocks_continuation(tmp_path):
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "rtl_agent_summary.txt").write_text(
        "Icarus compile: fail\nVerilator lint: fail\nIssue count: 2\n",
        encoding="utf-8",
    )

    gate = _load_gate()
    failure = gate("arch2rtl", {}, str(tmp_path))
    assert failure and "final tool checks failed" in failure


def test_arch2rtl_repair_summary_is_authoritative(tmp_path):
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "rtl_agent_summary.txt").write_text(
        "Icarus compile: fail\nVerilator lint: fail\n",
        encoding="utf-8",
    )
    (rtl_dir / "rtl_agent_summary_pass2.txt").write_text(
        "Icarus compile: pass\nVerilator lint: pass\nIssue count: 0\n",
        encoding="utf-8",
    )

    gate = _load_gate()
    assert gate("arch2rtl", {}, str(tmp_path)) is None


def test_arch2rtl_gate_finds_outputs_at_workflow_root(tmp_path):
    """The runner passes <workflow>/arch2rtl but agents save to <workflow>/rtl."""
    execution_dir = tmp_path / "arch2rtl"
    execution_dir.mkdir()
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "rtl_agent_summary.txt").write_text(
        "Icarus compile: pass\nVerilator lint: pass\nIssue count: 0\n",
        encoding="utf-8",
    )

    gate = _load_gate()
    assert gate("arch2rtl", {"status": "Spec2RTL conformance partial"}, str(execution_dir)) is None
