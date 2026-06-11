import json

from agents.digital import digital_dqa_summary_agent as dqa_summary_agent
from agents.digital.digital_cdc_analysis_agent import _rtl_files_from_state as cdc_rtl_files_from_state
from agents.digital.digital_reset_integrity_agent import _rtl_files_from_state as reset_rtl_files_from_state
from agents.digital.digital_rtl_linting_agent import _rtl_files_from_state as lint_rtl_files_from_state


def test_dqa_summary_does_not_require_tapeout_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(dqa_summary_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    digital_dir = tmp_path / "digital"
    signoff_dir = tmp_path / "signoff"
    digital_dir.mkdir()
    signoff_dir.mkdir()
    (digital_dir / "rtl_lint_report.json").write_text(json.dumps({
        "status": "pass",
        "rtl_file_count": 2,
        "summary": {"heuristic_issue_count": 0},
    }), encoding="utf-8")
    (digital_dir / "cdc_findings.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    (digital_dir / "reset_integrity_findings.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    (signoff_dir / "synthesis_readiness_findings.json").write_text(json.dumps({
        "rtl_file_count": 2,
        "score": 95,
        "yosys": {"errors": [], "warnings": []},
    }), encoding="utf-8")

    state = dqa_summary_agent.run_agent({"workflow_id": "wf", "workflow_dir": str(tmp_path)})

    assert state["digital"]["dqa_summary"]["status"] == "pass"
    assert state["digital"]["dqa_summary"]["reports"]["rtl_lint"].endswith("rtl_lint_report.json")


def test_dqa_rtl_collectors_prefer_state_and_exclude_vv(tmp_path):
    rtl = tmp_path / "system" / "imported_rtl" / "top.sv"
    sva = tmp_path / "vv" / "tb" / "top_assertions.sv"
    rtl.parent.mkdir(parents=True)
    sva.parent.mkdir(parents=True)
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    sva.write_text("module top_assertions; endmodule\n", encoding="utf-8")
    state = {"rtl_files": [str(rtl), str(sva)]}

    assert lint_rtl_files_from_state(state, str(tmp_path)) == [str(rtl)]
    assert cdc_rtl_files_from_state(state, str(tmp_path)) == [str(rtl)]
    assert reset_rtl_files_from_state(state, str(tmp_path)) == [str(rtl)]
