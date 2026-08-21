import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.embedded import _embedded_common as common
from agents.embedded import embedded_cocotb_harness_agent
from agents.embedded import embedded_co_sim_runner_agent
from agents.embedded import embedded_digital_handoff_ingest_agent
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


def test_rust_hal_uses_64_bit_mmio_for_wide_register_fields():
    regmap = {
        "base_address": "0x0",
        "registers": [{
            "name": "CSR_CFG0",
            "offset": "0x0",
            "access": "RW",
            "fields": [
                {"name": "LOW", "bit_offset": 0, "bit_width": 32, "access": "RW"},
                {"name": "HIGH", "bit_offset": 32, "bit_width": 32, "access": "RW"},
            ],
        }],
    }

    rust = embedded_rust_register_layer_generator_agent._default_hal_from_regmap(regmap)

    assert "CSR_CFG0_HIGH_MASK: u64 = 0xFFFFFFFF00000000" in rust
    assert "fn read_reg(offset: usize) -> u64" in rust
    assert "fn write_reg(offset: usize, value: u64)" in rust
    assert "pub fn read_csr_cfg0() -> u64" in rust


def test_rust_hal_preserves_32_bit_mmio_for_narrow_registers():
    regmap = {
        "base_address": "0x0",
        "registers": [{
            "name": "CTRL",
            "offset": "0x0",
            "access": "RW",
            "fields": [{"name": "ENABLE", "bit_offset": 0, "bit_width": 1, "access": "RW"}],
        }],
    }

    rust = embedded_rust_register_layer_generator_agent._default_hal_from_regmap(regmap)

    assert "CTRL_ENABLE_MASK: u32 = 0x00000001" in rust
    assert "fn read_reg(offset: usize) -> u32" in rust


def test_parent_read_only_register_overrides_incorrect_writable_field_access():
    regmap = {
        "base_address": "0x40000000",
        "registers": [{
            "name": "STATUS",
            "offset": "0x4",
            "access": "RO",
            "fields": [{"name": "READY", "bit_offset": 0, "bit_width": 1, "access": "RW"}],
        }],
    }

    rust = embedded_rust_register_layer_generator_agent._default_hal_from_regmap(regmap)

    assert "pub fn read_status()" in rust
    assert "pub fn get_status_ready()" in rust
    assert "write_status" not in rust
    assert "set_status_ready" not in rust
    report = embedded_register_validation_agent._validate(regmap, rust, "")
    assert report["status"] == "pass"


def test_embedded_handoff_ingests_regmap_from_system_rtl_package(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "save_text_artifact_and_record", lambda **_kwargs: None)

    source_id = "source-system-rtl"
    package_path = f"backend/workflows/{source_id}/system/package/system_rtl_package.json"
    rtl_path = f"backend/workflows/{source_id}/system/integration/temp_monitor_soc.sv"
    regmap_path = f"backend/workflows/{source_id}/system/package/digital_regmap.json"

    package = {
        "storage": {
            "rtl_files": ["system/integration/temp_monitor_soc.sv"],
            "digital_regmap": "system/package/digital_regmap.json",
        },
        "ready_for_cosim": True,
    }
    regmap = {
        "block_name": "temp_monitor",
        "base_address": "0x40000000",
        "registers": [
            {
                "name": "STATUS",
                "offset": "0x4",
                "access": "RO",
                "fields": [{"name": "ready", "bit_offset": 0, "bit_width": 1}],
            }
        ],
    }
    rtl = b"""
module temp_monitor_soc(input logic clk, input logic rst_n, output logic irq);
  assign irq = rst_n;
endmodule
"""
    blobs = {
        package_path: json.dumps(package).encode("utf-8"),
        rtl_path: rtl,
        regmap_path: json.dumps(regmap).encode("utf-8"),
    }

    class Response:
        def __init__(self, data):
            self.data = data

    class Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def single(self):
            return self

        def execute(self):
            return Response({"id": source_id, "artifacts": {"pkg": package_path}})

    class Bucket:
        def download(self, path):
            if path in blobs:
                return blobs[path]
            raise FileNotFoundError(path)

        def list(self, _path):
            return []

    class Storage:
        def from_(self, _bucket):
            return Bucket()

    class Client:
        storage = Storage()

        def table(self, _name):
            return Query()

    state = {
        "workflow_id": "firmware-wf",
        "workflow_dir": str(tmp_path),
        "supabase_client": Client(),
        "from_workflow_id": source_id,
        "top_module": "temp_monitor_soc",
    }

    embedded_digital_handoff_ingest_agent.run_agent(state)

    assert state["digital_regmap"]["registers"][0]["name"] == "STATUS"
    assert state["digital_regmap_path"].replace("\\", "/").endswith("digital/digital_regmap.json")
    assert (tmp_path / "digital" / "digital_regmap.json").is_file()
    assert state["system_rtl_package"]["register_map_path"] == "digital/digital_regmap.json"


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

@pytest.mark.parametrize(
    ("configured_target", "target_isa", "expected"),
    [
        ("rv32imc-unknown-none-elf", "rv32imc", "riscv32imc-unknown-none-elf"),
        ("rv32i", "rv32i", "riscv32i-unknown-none-elf"),
        ("riscv32-unknown-none-elf", "rv32im", "riscv32im-unknown-none-elf"),
        ("riscv32imc-unknown-none-elf", "rv32imc", "riscv32imc-unknown-none-elf"),
        ("thumbv7em-none-eabihf", "", "thumbv7em-none-eabihf"),
        ("custom-target.json", "", "custom-target.json"),
    ],
)
def test_elf_builder_canonicalizes_riscv_rust_targets(configured_target, target_isa, expected):
    state = {"toolchain": {"target_triple": configured_target, "target_isa": target_isa}}

    target, _ = embedded_elf_build_agent._resolve_toolchain(state, {})

    assert target == expected


def test_elf_builder_installs_missing_standard_target_and_retries(tmp_path, monkeypatch):
    workspace = tmp_path / "firmware" / "build"
    workspace.mkdir(parents=True)
    calls = []

    class Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    results = iter([
        Result(1, stderr="error[E0463]: can't find crate for `core`; target may not be installed"),
        Result(0, stdout="installed"),
        Result(0, stdout="compiled"),
    ])

    def fake_run(_state, capability, command, **kwargs):
        calls.append((capability, command, kwargs))
        return next(results)

    monkeypatch.setattr(embedded_elf_build_agent, "run_command", fake_run)
    monkeypatch.setattr(embedded_elf_build_agent.shutil, "which", lambda name: "/usr/bin/rustup" if name == "rustup" else None)

    attempted, succeeded, stdout, stderr, _ = embedded_elf_build_agent._attempt_build(
        str(tmp_path), "riscv32imc-unknown-none-elf", "firmware_app", "/usr/bin/cargo"
    )

    assert attempted is True
    assert succeeded is True
    assert [call[0] for call in calls] == [
        "embedded_firmware_build",
        "embedded_rust_target_install",
        "embedded_firmware_build_retry",
    ]
    assert calls[1][1] == ["/usr/bin/rustup", "target", "add", "riscv32imc-unknown-none-elf"]
    assert "compiled" in stdout
    assert "can't find crate" in stderr


def test_elf_builder_does_not_install_custom_json_target(tmp_path, monkeypatch):
    workspace = tmp_path / "firmware" / "build"
    workspace.mkdir(parents=True)
    target = tmp_path / "custom.json"
    target.write_text("{}", encoding="utf-8")
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "error[E0463]: can't find crate for `core`; target may not be installed"

    def fake_run(_state, capability, command, **kwargs):
        calls.append((capability, command))
        return Result()

    monkeypatch.setattr(embedded_elf_build_agent, "run_command", fake_run)
    monkeypatch.setattr(embedded_elf_build_agent.shutil, "which", lambda _name: "/usr/bin/rustup")

    _, succeeded, _, _, _ = embedded_elf_build_agent._attempt_build(
        str(tmp_path), "custom.json", "firmware_app", "/usr/bin/cargo"
    )

    assert succeeded is False
    assert [call[0] for call in calls] == ["embedded_firmware_build"]


def test_elf_builder_finds_rustup_beside_profile_resolved_cargo(tmp_path, monkeypatch):
    workspace = tmp_path / "firmware" / "build"
    workspace.mkdir(parents=True)
    tool_bin = tmp_path / "toolchain" / "bin"
    tool_bin.mkdir(parents=True)
    cargo = tool_bin / "cargo"
    rustup = tool_bin / "rustup"
    cargo.write_text("", encoding="utf-8")
    rustup.write_text("", encoding="utf-8")
    rustup.chmod(0o755)
    calls = []

    class Result:
        def __init__(self, returncode, stderr=""):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    results = iter([
        Result(1, "can't find crate for `core`; target may not be installed"),
        Result(0),
        Result(0),
    ])

    def fake_run(_state, capability, command, **_kwargs):
        calls.append((capability, command))
        return next(results)

    monkeypatch.setattr(embedded_elf_build_agent, "run_command", fake_run)
    monkeypatch.setattr(embedded_elf_build_agent.shutil, "which", lambda _name: None)
    monkeypatch.setattr(embedded_elf_build_agent, "tool_path", lambda _name: None)

    _, succeeded, _, _, _ = embedded_elf_build_agent._attempt_build(
        str(tmp_path), "riscv32imc-unknown-none-elf", "firmware_app", str(cargo)
    )

    assert succeeded is True
    assert calls[1][1] == [str(rustup), "target", "add", "riscv32imc-unknown-none-elf"]


def test_backend_image_preinstalls_embedded_rust_toolchain_matrix():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "RUSTUP_HOME=/opt/rustup" in dockerfile
    assert "CARGO_HOME=/opt/cargo" in dockerfile
    assert "riscv32imc-unknown-none-elf" in dockerfile
    assert "riscv64gc-unknown-none-elf" in dockerfile
    assert "thumbv7em-none-eabihf" in dockerfile
    assert "/opt/rustup /opt/cargo" in dockerfile


def test_backend_image_pins_esp_idf_for_ulx3s_onboard_cpu():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "IDF_PATH=/opt/esp-idf" in dockerfile
    assert "--branch v5.5.3" in dockerfile
    assert "/opt/esp-idf/install.sh esp32" in dockerfile
