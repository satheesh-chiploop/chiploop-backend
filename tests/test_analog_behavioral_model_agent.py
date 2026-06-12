import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.analog import analog_behavioral_model_agent as agent


def test_sanitize_removes_empty_comb_block_after_seq_assignment_cleanup():
    model = """
module temp_sensor_adc_model(input sample_req, output reg busy);
reg [7:0] latency_counter;

always @(*) begin
    if (!busy)
        latency_counter = 8'd3;
    else if (latency_counter <= 0)
        busy = 1'b0;
    else
        latency_counter = latency_counter - 1'b1;
end

always @(posedge sample_req or negedge sample_req) begin
    if (!sample_req) begin
        latency_counter <= 8'd0;
        busy <= 1'b0;
    end else begin
        latency_counter <= 8'd3;
        busy <= 1'b1;
    end
end

endmodule
"""

    sanitized = agent._sanitize_verilog2005_model(model)

    assert "if (!busy)" not in sanitized
    assert "else if (latency_counter <= 0)" not in sanitized
    assert "always @(posedge sample_req or negedge sample_req)" in sanitized
    assert "latency_counter <= 8'd3;" in sanitized


def test_run_agent_records_pass1_and_pass2_artifacts(tmp_path, monkeypatch):
    calls = {"llm": 0, "compile": 0}
    saved = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_llm(_prompt: str) -> str:
        calls["llm"] += 1
        if calls["llm"] == 1:
            return "module ana(input vin, output reg vout);\nalways @(*) begin\n  vout = vin;\nend\nendmodule\n"
        return "module ana(input vin, output reg vout);\nalways @(*) begin\n  vout = vin;\nend\nendmodule\n"

    def fake_run_command(_state, capability, _cmd):
        if capability in {"analog_behavioral_iverilog_compile", "analog_behavioral_iverilog_compile_repair"}:
            calls["compile"] += 1
            if calls["compile"] == 1:
                return Result(1, "", "pass1 syntax error")
            return Result(0, "pass2 ok", "")
        if capability == "analog_behavioral_verilator_lint":
            return Result(0, "verilator ok", "")
        return Result(0, "", "")

    monkeypatch.setattr(agent, "llm_text", fake_llm)
    monkeypatch.setattr(agent, "run_command", fake_run_command)
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: saved.append(args))

    state = agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_spec": {
            "block_name": "ana",
            "module_name": "ana",
            "ports": [
                {"name": "vin", "direction": "input", "width": 1},
                {"name": "vout", "direction": "output", "width": 1},
            ],
        },
    })

    analog_dir = tmp_path / "analog"
    assert (analog_dir / "ana_pass1.v").exists()
    assert (analog_dir / "ana_compile_pass1.log").read_text(encoding="utf-8").strip() == "pass1 syntax error"
    assert (analog_dir / "ana_pass2.v").exists()
    assert (analog_dir / "ana_compile_pass2.log").read_text(encoding="utf-8").strip() == "pass2 ok"
    assert (analog_dir / "ana_verilator_lint_pass2.log").read_text(encoding="utf-8").strip() == "verilator ok"
    summary_text = (analog_dir / "ana_compile_summary.json").read_text(encoding="utf-8")
    assert '"repair_used": true' in summary_text
    assert '"pass2_model": "ana_pass2.v"' in summary_text
    saved_names = [args[3] for args in saved]
    assert "ana_pass1.v" in saved_names
    assert "ana_compile_pass1.log" in saved_names
    assert "ana_pass2.v" in saved_names
    assert "ana_compile_pass2.log" in saved_names
    assert state["analog_model_file"] == "ana.v"
