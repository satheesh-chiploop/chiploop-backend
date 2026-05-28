import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_firmware_cosim_execution_agent as agent


def test_cocotb_execution_uses_python_env_paths(tmp_path, monkeypatch):
    validate_dir = tmp_path / "firmware" / "validate"
    validate_dir.mkdir(parents=True)
    makefile = validate_dir / "Makefile"
    makefile.write_text("all:\n\t@echo ok\n", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/make" if name == "make" else None)

    def fake_run(cmd, cwd, env, capture_output, text, timeout):
        captured.update({"cmd": cmd, "cwd": cwd, "env": env})

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    result = agent._run_cocotb_simulation(str(tmp_path), "firmware/validate/Makefile", "test_firmware_smoke")

    assert result["success"] is True
    path_entries = captured["env"]["PATH"].split(os.pathsep)
    pythonpath_entries = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert path_entries[0] == os.path.dirname(sys.executable)
    assert str(validate_dir) in pythonpath_entries
    assert str(tmp_path) in pythonpath_entries
