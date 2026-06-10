import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_agent as agent


def test_verilator_lint_preserves_pass2_relative_subdir(tmp_path, monkeypatch):
    rtl_dir = tmp_path / "rtl"
    pass2_dir = rtl_dir / "pass2"
    pass2_dir.mkdir(parents=True)
    rtl_file = pass2_dir / "temp_monitor_digital.v"
    rtl_file.write_text("module temp_monitor_digital; endmodule\n", encoding="utf-8")
    captured = {}

    class Result:
        stdout = ""
        stderr = ""
        error = ""
        returncode = 0
        status = "ok"

        def to_dict(self):
            return {"returncode": self.returncode, "status": self.status}

    def fake_run_tool(state, capability, tool, args, cwd=None, metadata=None):
        captured["args"] = args
        captured["cwd"] = cwd
        return Result()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "run_tool", fake_run_tool)

    ok, _path, _output, _result = agent._run_verilator_lint(
        rtl_dir=str(rtl_dir),
        verilog_files=[str(rtl_file.relative_to(tmp_path))],
        top_module="temp_monitor_digital",
        suffix="pass2",
        state={},
    )

    assert ok is True
    assert captured["cwd"] == str(rtl_dir)
    assert "pass2/temp_monitor_digital.v" in captured["args"]
    assert "temp_monitor_digital.v" not in captured["args"][-1:]
