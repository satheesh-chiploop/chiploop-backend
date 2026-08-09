from pathlib import Path

import pytest

from agents.fpga import fpga_logic_equivalence_agent as lec


def _state(tmp_path: Path) -> dict:
    rtl = tmp_path / "top.sv"
    netlist = tmp_path / "top_ice40_netlist.v"
    rtl.write_text("module top(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n", encoding="utf-8")
    netlist.write_text(rtl.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "board": "icebreaker",
        "run_fpga_lec": True,
        "require_fpga_lec": True,
        "fpga": {
            "top_module": "top",
            "rtl_files": [str(rtl)],
            "synthesis": {"status": "completed", "verilog_netlist": str(netlist)},
        },
    }


def test_fpga_lec_uses_yosys_equivalence_and_passes(tmp_path, monkeypatch):
    state = _state(tmp_path)
    published = {}
    monkeypatch.setattr(lec, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    monkeypatch.setattr(lec, "manifest_update", lambda state, key, value: state["fpga"].__setitem__(key, value))

    def fake_run(_cmd, cwd, log_path, **_kwargs):
        Path(log_path).write_text("Equivalence successfully proven!\n", encoding="utf-8")
        return {"ok": True, "cmd": ["yosys"]}

    monkeypatch.setattr(lec, "run_cmd", fake_run)
    lec.run_agent(state)

    assert published["status"] == "pass"
    assert published["tool"] == "Yosys"
    script = Path(published["script"]).read_text(encoding="utf-8")
    assert "equiv_make gold gate equiv" in script
    assert "equiv_simple -undef -seq 20" in script
    assert "equiv_induct -undef -seq 12" in script
    assert "equiv_induct -undef -seq 24" in script
    assert "equiv_induct -undef -seq 48" in script
    assert "equiv_status -assert" in script
    assert published["induction_depths_attempted"] == [12, 24, 48]


def test_fpga_lec_disabled_by_user_is_recorded(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state["run_fpga_lec"] = False
    published = {}
    monkeypatch.setattr(lec, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    monkeypatch.setattr(lec, "manifest_update", lambda *_args: None)
    monkeypatch.setattr(lec, "run_cmd", lambda *_args, **_kwargs: pytest.fail("Yosys must not run when LEC is disabled"))

    lec.run_agent(state)
    assert published["status"] == "disabled"
    assert published["reason"] == "FPGA LEC disabled by user."


def test_required_fpga_lec_failure_blocks_downstream(tmp_path, monkeypatch):
    state = _state(tmp_path)
    monkeypatch.setattr(lec, "publish_json", lambda *_args: None)
    monkeypatch.setattr(lec, "manifest_update", lambda *_args: None)
    monkeypatch.setattr(lec, "run_cmd", lambda *_args, **_kwargs: {"ok": False, "stderr_tail": "unproven equivalence"})

    with pytest.raises(RuntimeError, match="LEC did not pass"):
        lec.run_agent(state)


def test_unproven_equivalence_is_reported_as_inconclusive(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state["require_fpga_lec"] = False
    published = {}
    monkeypatch.setattr(lec, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    monkeypatch.setattr(lec, "manifest_update", lambda *_args: None)

    def incomplete_run(_cmd, cwd, log_path, **_kwargs):
        Path(log_path).write_text(
            "Found 14 unproven $equiv cells in module equiv.\n"
            "ERROR: Found 9 unproven $equiv cells in 'equiv_status -assert'.\n",
            encoding="utf-8",
        )
        return {"ok": False, "stderr_tail": "ERROR: Found 9 unproven $equiv cells"}

    monkeypatch.setattr(lec, "run_cmd", incomplete_run)
    lec.run_agent(state)
    assert published["status"] == "inconclusive"
    assert published["failure_kind"] == "proof_incomplete"
    assert published["unproven_points"] == 9
    assert "12, 24, 48" in published["reason"]

def test_fpga_lec_is_registered_in_supabase_and_all_implementation_flows():
    root = Path(__file__).parents[2]
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")
    migration = (root / "backend" / "supabase" / "migrations" / "phase_20260730_fpga_rtl_netlist_lec.sql").read_text(encoding="utf-8")
    frontend = (root / "frontend" / "app" / "apps" / "digital-review" / "_DigitalReviewAppTemplate.tsx").read_text(encoding="utf-8")

    assert '"FPGA RTL-to-Netlist Equivalence Agent": fpga_logic_equivalence_agent' in main
    assert main.count('"FPGA RTL-to-Netlist Equivalence Agent",') >= 4
    assert "'defaultValue',true" in migration
    assert "run_fpga_lec" in migration
    assert "runFpgaLec" in frontend
