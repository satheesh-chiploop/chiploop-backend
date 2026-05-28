import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_simulation_execution_agent as execution_agent
from agents.digital import digital_formal_verification_agent as formal_agent
from agents.digital import digital_simulation_summary_coverage_agent as summary_agent
from agents.digital import digital_testbench_generator_agent as tb_agent


def test_verilator_makefile_enables_code_coverage():
    text = tb_agent._gen_makefile("pwm_controller")

    assert "override SIM := verilator" in text
    assert "EXTRA_ARGS += --coverage" in text


def test_parse_lcov_info_reports_line_and_branch_coverage(tmp_path):
    info = tmp_path / "code_coverage.info"
    info.write_text(
        "\n".join(
            [
                "SF:rtl/pwm_controller.v",
                "LF:10",
                "LH:8",
                "BRF:4",
                "BRH:3",
                "end_of_record",
            ]
        ),
        encoding="utf-8",
    )

    parsed = execution_agent._parse_lcov_info(str(info))

    assert parsed["line_coverage_pct"] == 80.0
    assert parsed["branch_coverage_pct"] == 75.0


def test_formal_sby_uses_selected_solver():
    text = formal_agent._gen_sby("pwm_controller", ["rtl/pwm_controller.v"], "clk", None, "boolector")

    assert "smtbmc boolector" in text


def test_formal_sby_paths_are_relative_to_formal_workdir(tmp_path):
    workflow_dir = tmp_path / "backend" / "workflows" / "wf"
    rtl = workflow_dir / "handoff" / "rtl" / "pwm_controller.v"
    formal_dir = workflow_dir / "vv" / "formal"
    rtl.parent.mkdir(parents=True)
    formal_dir.mkdir(parents=True)
    rtl.write_text("module pwm_controller; endmodule\n", encoding="utf-8")

    text = formal_agent._gen_sby("pwm_controller", [str(rtl)], "clk", None, "z3", str(formal_dir))

    assert "backend/workflows/wf/handoff" not in text
    assert "../../handoff/rtl/pwm_controller.v" in text.replace("\\", "/")


def test_summary_agent_includes_code_assertion_formal_and_golden_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(summary_agent, "_record_text", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifact").mkdir()

    workflow_dir = tmp_path / "workflow"
    reports_dir = workflow_dir / "vv" / "tb" / "reports"
    run_logs_dir = reports_dir / "run_logs"
    run_logs_dir.mkdir(parents=True)
    (reports_dir / "simulation_execution_summary.json").write_text(
        json.dumps({"total": 2, "pass": 2, "fail": 0}),
        encoding="utf-8",
    )
    (reports_dir / "functional_coverage_summary.json").write_text(
        json.dumps({"functional_coverage_pct": 66.67, "bins_hit": 4, "total_bins": 6}),
        encoding="utf-8",
    )
    (reports_dir / "code_coverage_summary.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "line_coverage_pct": 81.25,
                "line_hit": 13,
                "line_found": 16,
                "branch_coverage_pct": 50.0,
                "branch_hit": 1,
                "branch_found": 2,
            }
        ),
        encoding="utf-8",
    )
    sva_path = workflow_dir / "vv" / "tb" / "pwm_sva.sv"
    sva_path.write_text(
        "a_one: assert property(p_one);\na_two: assert property(p_two);\n",
        encoding="utf-8",
    )

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(workflow_dir),
        "sva_assertions_path": str(sva_path),
        "vv": {
            "formal": {"run": {"available": True, "attempted": True, "returncode": 0}},
            "golden_model": {"top_module": "pwm_controller"},
        },
    }

    summary_agent.run_agent(state)

    summary = json.loads((reports_dir / "simulation_summary_coverage.json").read_text(encoding="utf-8"))
    assert summary["coverage"]["functional"]["coverage_pct"] == 66.67
    assert summary["coverage"]["code"]["line_coverage_pct"] == 81.25
    assert summary["coverage"]["assertions"]["assertions_generated"] == 2
    assert summary["coverage"]["assertions"]["assertion_pass_pct"] == 100.0
    assert summary["formal"]["status"] == "pass"
    assert summary["golden_model"]["status"] == "generated"
    assert summary["toolchain"]["simulator"] == "verilator"
    assert summary["toolchain"]["code_coverage"] == "verilator_coverage"
