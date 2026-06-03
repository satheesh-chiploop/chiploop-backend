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

    state = {
        "tool_profile": {
            "profile_id": "test",
            "strict_tool_profile": True,
            "tools": {"make": {"executable": "/usr/bin/make"}},
        }
    }

    def fake_run(state, adapter_id, command, cwd=None, env=None, timeout_sec=None):
        captured.update({
            "state": state,
            "adapter_id": adapter_id,
            "cmd": command,
            "cwd": cwd,
            "env": env,
            "timeout_sec": timeout_sec,
        })

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

            def to_dict(self):
                return {"returncode": self.returncode}

        return Result()

    monkeypatch.setattr(agent, "run_command", fake_run)

    result = agent._run_cocotb_simulation(state, str(tmp_path), "firmware/validate/Makefile", "test_firmware_smoke")

    assert result["success"] is True
    path_entries = captured["env"]["PATH"].split(os.pathsep)
    pythonpath_entries = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert path_entries[0] == os.path.dirname(sys.executable)
    assert str(validate_dir) in pythonpath_entries
    assert str(tmp_path) in pythonpath_entries
    assert captured["adapter_id"] == "system_firmware_cosim"
