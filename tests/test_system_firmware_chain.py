import json
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.embedded import _embedded_common as common
from agents.embedded import embedded_cocotb_harness_agent
from agents.embedded import embedded_co_sim_runner_agent
from agents.embedded import embedded_elf_build_agent
from agents.embedded import embedded_firmware_executive_summary_agent
from agents.embedded import embedded_firmware_integration_contract_agent
from agents.embedded import embedded_firmware_register_extract_agent
from agents.embedded import embedded_interrupt_mapping_agent
from agents.embedded import embedded_register_validation_agent
from agents.embedded import embedded_rust_driver_scaffold_agent
from agents.embedded import embedded_rust_register_layer_generator_agent
from agents.embedded import embedded_validation_report_agent
from agents.embedded import embedded_verilator_build_agent
from agents.system import system_firmware_cosim_execution_agent
from agents.system import system_firmware_coverage_summary_agent


def test_system_firmware_pwm_like_chain_reaches_cosim_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "save_text_artifact_and_record", lambda **_kwargs: None)
    monkeypatch.setattr(system_firmware_cosim_execution_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(system_firmware_coverage_summary_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(embedded_elf_build_agent, "tool_path", lambda name, state=None: None)
    monkeypatch.setattr(embedded_firmware_register_extract_agent, "llm_chat", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(embedded_rust_register_layer_generator_agent, "llm_chat", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(embedded_rust_driver_scaffold_agent, "llm_chat", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        embedded_firmware_integration_contract_agent,
        "llm_chat",
        lambda *_args, **_kwargs: "# Firmware Integration Contract\n",
    )
    monkeypatch.setattr(
        embedded_co_sim_runner_agent,
        "llm_chat",
        lambda *_args, **_kwargs: (
            "FILE: firmware/validate/cosim_run.md\n"
            "# Co-simulation run\n"
            "FILE: firmware/validate/run_cosim.sh\n"
            "make -f firmware/validate/Makefile\n"
        ),
    )

    system_dir = tmp_path / "system" / "integration"
    rtl_dir = tmp_path / "digital" / "rtl"
    digital_dir = tmp_path / "digital"
    system_dir.mkdir(parents=True)
    rtl_dir.mkdir(parents=True)

    soc_top = system_dir / "pwm_soc_sim.sv"
    pwm_rtl = rtl_dir / "pwm_controller.v"
    filelist = system_dir / "system_rtl_filelist_sim.txt"
    regmap_path = digital_dir / "digital_regmap.json"

    soc_top.write_text(
        """
module pwm_soc_sim(input logic clk, input logic rst_n, output logic pwm_out);
  pwm_controller u_pwm(.clk(clk), .rst_n(rst_n), .pwm_out(pwm_out));
endmodule
""",
        encoding="utf-8",
    )
    pwm_rtl.write_text(
        """
module pwm_controller(input clk, input rst_n, output pwm_out);
  assign pwm_out = rst_n;
endmodule
""",
        encoding="utf-8",
    )
    filelist.write_text(f"{soc_top}\n{pwm_rtl}\n", encoding="utf-8")
    regmap_path.write_text(
        json.dumps(
            {
                "block_name": "pwm_controller",
                "base_address": "0x40000000",
                "registers": [
                    {
                        "name": "CTRL",
                        "offset": "0x0",
                        "access": "RW",
                        "fields": [{"name": "enable", "bit_offset": 0, "bit_width": 1}],
                    },
                    {
                        "name": "IRQ_STATUS",
                        "offset": "0x4",
                        "access": "RO",
                        "fields": [{"name": "done_irq", "bit_offset": 0, "bit_width": 1, "access": "RO"}],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    state = {
        "workflow_id": "system-firmware-pwm",
        "workflow_dir": str(tmp_path),
        "spec_text": "PWM controller firmware",
        "soc_top_sim_path": "system/integration/pwm_soc_sim.sv",
        "system_top_sim_path": "system/integration/pwm_soc_sim.sv",
        "system_rtl_filelist_sim": "system/integration/system_rtl_filelist_sim.txt",
        "top_module": "pwm_soc_sim",
    }

    for run in (
        embedded_firmware_register_extract_agent.run_agent,
        embedded_rust_register_layer_generator_agent.run_agent,
        embedded_register_validation_agent.run_agent,
        embedded_rust_driver_scaffold_agent.run_agent,
        embedded_interrupt_mapping_agent.run_agent,
        embedded_firmware_integration_contract_agent.run_agent,
        embedded_elf_build_agent.run_agent,
        embedded_verilator_build_agent.run_agent,
        embedded_cocotb_harness_agent.run_agent,
        embedded_co_sim_runner_agent.run_agent,
        system_firmware_cosim_execution_agent.run_agent,
        system_firmware_coverage_summary_agent.run_agent,
        embedded_validation_report_agent.run_agent,
        embedded_firmware_executive_summary_agent.run_agent,
    ):
        run(state)
        assert not str(state.get("status") or "").startswith("❌")

    execution = state["system_firmware_execution"]
    assert execution["overall_status"] == "ready_for_execution"
    assert execution["readiness"]["status"] == "ready"
    assert execution["inputs"]["soc_top_sim_path"] == "system/integration/pwm_soc_sim.sv"
    assert execution["inputs"]["makefile_path"] == "firmware/validate/Makefile"
    assert execution["inputs"]["test_paths"] == ["firmware/validate/test_firmware_smoke.py"]
    assert execution["inputs"]["firmware_elf_placeholder"] is True
    assert state["system_firmware_coverage_summary"]["coverage_metrics"]["coverage_available"] is False
