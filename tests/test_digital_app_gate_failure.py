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


def test_arch2rtl_gate_prefers_explicit_execution_state(tmp_path):
    gate = _load_gate()
    state = {
        "status": "Spec2RTL conformance partial",
        "rtl_quality_gate": {
            "passed": True,
            "compile_passed": True,
            "lint_passed": True,
            "final_pass": "pass2",
        },
    }
    assert gate("arch2rtl", state, str(tmp_path / "arch2rtl")) is None


def test_arch2rtl_gate_blocks_explicit_lint_failure(tmp_path):
    gate = _load_gate()
    state = {
        "rtl_quality_gate": {
            "passed": False,
            "compile_passed": True,
            "lint_passed": False,
            "final_pass": "pass2",
        },
    }
    assert "lint=fail" in gate("arch2rtl", state, str(tmp_path / "arch2rtl"))


def test_verify_gate_uses_explicit_simulation_result(tmp_path):
    gate = _load_gate()
    state = {
        "verification_quality_gate": {
            "passed": True,
            "total": 8,
            "pass": 8,
            "fail": 0,
            "simulator": "verilator",
        }
    }
    assert gate("verify", state, str(tmp_path / "verify")) is None


def test_verify_gate_uses_legacy_shared_execution_report(tmp_path):
    gate = _load_gate()
    state = {"vv": {"simulation_execution": {"pass": 8, "fail": 0}}}
    assert gate("verify", state, str(tmp_path / "verify")) is None


def test_verify_gate_blocks_explicit_simulation_failure(tmp_path):
    gate = _load_gate()
    state = {
        "verification_quality_gate": {
            "passed": False,
            "total": 8,
            "pass": 7,
            "fail": 1,
        }
    }
    assert gate("verify", state, str(tmp_path / "verify")) == "verification failed (7 passed, 1 failed)"


def test_fpga_explorer_gate_requires_selected_board(tmp_path):
    gate = _load_gate()
    passing = {
        "fpga_target_explorer": {
            "status": "completed",
            "selected_recommendation": "orangecrab_ecp5_85f",
        }
    }
    assert gate("fpga_target_explorer", passing, str(tmp_path / "fpga_target_explorer")) is None

    missing_board = {"fpga_target_explorer": {"status": "completed", "selected_recommendation": None}}
    assert "did not select" in gate("fpga_target_explorer", missing_board, str(tmp_path / "fpga_target_explorer"))


def test_fpga_bitstream_gate_requires_real_artifact(tmp_path):
    gate = _load_gate()
    passing = {
        "generate_bitstream": True,
        "fpga": {
            "bitstream": {
                "status": "completed",
                "artifact_produced": True,
                "bitstream": "/workflow/fpga/bitstream/design.bit",
            }
        },
    }
    assert gate("fpga", passing, str(tmp_path / "fpga_bitstream")) is None

    missing = {"generate_bitstream": True, "fpga": {"bitstream": {"status": "failed"}}}
    assert "was not produced" in gate("fpga", missing, str(tmp_path / "fpga_bitstream"))


def test_tapeout_gate_requires_explicit_gds_result(tmp_path):
    gate = _load_gate()
    passing = {
        "digital": {
            "synth": {"status": "ok", "netlist": "/workflow/netlist.v"},
            "tapeout": {"status": "ok", "gds_klayout": "/workflow/design.gds"},
        }
    }
    assert gate("arch2tapeout", passing, str(tmp_path / "arch2tapeout")) is None

    missing_gds = {
        "digital": {
            "synth": {"status": "ok", "netlist": "/workflow/netlist.v"},
            "tapeout": {"status": "ok"},
        }
    }
    assert gate("arch2tapeout", missing_gds, str(tmp_path / "arch2tapeout")) == "tapeout did not produce GDS"
