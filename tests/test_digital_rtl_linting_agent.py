import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_linting_agent as agent


def test_verilator_warning_codes_extracts_structural_warnings():
    stderr = """
%Warning-UNDRIVEN: top.sv:23:9: Signal is not driven
%Warning-WIDTHEXPAND: model.v:31:33: Operator ADD expects 64 bits
%Warning-MULTIDRIVEN: top.sv:24:16: Signal has multiple driving blocks
"""

    codes = agent._verilator_warning_codes(stderr)
    blocking = [
        code for code in codes
        if code in agent.SYNTHESIS_BLOCKING_VERILATOR_WARNINGS
    ]

    assert codes == ["UNDRIVEN", "WIDTHEXPAND", "MULTIDRIVEN"]
    assert blocking == ["UNDRIVEN", "MULTIDRIVEN"]


def test_rtl_file_discovery_prefers_pass2_duplicate_modules(tmp_path):
    rtl_dir = tmp_path / "rtl"
    pass2_dir = rtl_dir / "pass2"
    pass2_dir.mkdir(parents=True)
    (rtl_dir / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (pass2_dir / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (rtl_dir / "helper.v").write_text("module helper; endmodule\n", encoding="utf-8")

    selected = agent._rtl_files_from_state(
        {
            "rtl_files": [
                str(rtl_dir / "top.v"),
                str(pass2_dir / "top.v"),
                str(rtl_dir / "helper.v"),
            ]
        },
        str(tmp_path),
    )

    assert str(pass2_dir / "top.v") in selected
    assert str(rtl_dir / "top.v") not in selected
    assert str(rtl_dir / "helper.v") in selected


def test_rtl_lint_status_requires_icarus_and_verilator_pass(tmp_path, monkeypatch):
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "top.v").write_text("module top; endmodule\n", encoding="utf-8")

    class Result:
        def __init__(self, returncode: int):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

        def to_dict(self):
            return {"returncode": self.returncode}

    def fake_tool_available(tool, state):
        return tool in {"iverilog", "verilator"}

    def fake_run_tool(state, capability, tool, args, timeout_sec=None, metadata=None):
        if tool == "iverilog":
            return Result(1)
        if tool == "verilator":
            return Result(0)
        raise AssertionError(tool)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "tool_available", fake_tool_available)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "rtl_files": [str(rtl_dir / "top.v")],
    }

    agent.run_agent(state)

    report = (tmp_path / "digital" / "rtl_lint_report.json").read_text(encoding="utf-8")
    assert '"status": "fail"' in report
    assert '"icarus_compile_passed": false' in report
    assert '"verilator_lint_passed": true' in report
