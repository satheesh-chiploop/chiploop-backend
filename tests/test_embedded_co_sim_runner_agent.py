import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.embedded import embedded_co_sim_runner_agent as agent


def test_cosim_runner_preserves_shell_environment_placeholders(tmp_path, monkeypatch):
    prompts = []
    artifacts = []

    def fake_llm_chat(prompt, system=""):
        prompts.append(prompt)
        return (
            "FILE: firmware/validate/cosim_run.md\n"
            "<!-- ASSUMPTION: cocotb is installed. -->\n"
            "# Co-simulation run\n"
            "FILE: firmware/validate/run_cosim.sh\n"
            "RTL_TOP=${RTL_TOP:-pwm_controller}\n"
        )

    monkeypatch.setattr(agent, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(
        agent,
        "write_artifact",
        lambda state, path, content, key=None: artifacts.append((path, content, key)),
    )

    state = {
        "workflow_id": "embedded-wf",
        "workflow_dir": str(tmp_path),
        "spec_text": "PWM controller firmware",
    }
    result = agent.run_agent(state)

    assert "RTL_TOP=${RTL_TOP:-<RTL_TOP>}" in prompts[0]
    assert "RTL_DIR=${RTL_DIR:-<RTL_DIR>}" in prompts[0]
    assert "FILELIST=${FILELIST:-<FILELIST>}" in prompts[0]
    assert result["embedded"]["cosim_run"] == "firmware/validate/cosim_run.md"
    script = next(content for path, content, _key in artifacts if path == "firmware/validate/run_cosim.sh")
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
