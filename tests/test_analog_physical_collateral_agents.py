import os
import re
from types import SimpleNamespace

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.analog import analog_sky130_spice_netlist_agent as spice_agent
from agents.analog import analog_abstract_views_agent as abstract_agent
from agents.analog import analog_gds_generation_agent as gds_agent
from agents.analog import analog_physical_collateral_package_agent as package_agent
from agents.analog import analog_collateral_consistency_agent as consistency_agent
from agents.analog import analog_lef_extraction_agent as lef_agent
from agents.analog import analog_liberty_characterization_agent as lib_agent
from agents.analog import analog_macro_interface_contract_agent as contract_agent
from agents.analog import analog_macro_interface_validation_agent as validation_agent


def test_abstract_lib_stub_is_self_contained_and_rejects_malformed_pin_syntax():
    spec = {
        "module_name": "temp_sensor_adc_model",
        "clock_period_ns": 10,
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "sample_req", "direction": "input", "width": 1},
            {"name": "sensor_temp_celsius", "direction": "input", "width": 16},
            {"name": "adc_code", "direction": "output", "width": 12},
            {"name": "adc_valid", "direction": "output", "width": 1},
        ],
    }

    lib = abstract_agent._build_lib_stub(spec)
    assert "lu_table_template (delay_template_1x1)" in lib
    assert "lu_table_template (constraint_template_1x1)" in lib
    assert "input_threshold_pct_rise : 50.0 ;" in lib
    assert "output_threshold_pct_fall : 50.0 ;" in lib
    assert "slew_lower_threshold_pct_rise : 20.0 ;" in lib
    assert "slew_upper_threshold_pct_fall : 80.0 ;" in lib
    assert "type (sensor_temp_celsius_bus_t)" in lib
    assert "bus (sensor_temp_celsius)" in lib
    assert "bus_type : sensor_temp_celsius_bus_t" in lib
    assert "type (adc_code_bus_t)" in lib
    assert "bus (adc_code)" in lib
    assert 'pin ("adc_code[0]")' in lib
    assert abstract_agent._lib_stub_issues(lib, spec) == []

    malformed = (
        "library (temp_sensor_adc_model_lib) { cell (temp_sensor_adc_model) { "
        "pg_pin (VPWR) {} pg_pin (VGND) {} "
        "lu_table_template (delay_template_1x1) {} "
        "lu_table_template (constraint_template_1x1) {} "
        "pin (adc_code[0]) { direction : output ; } } }"
    )
    assert "unquoted bus bit pin name" in abstract_agent._lib_stub_issues(malformed, spec)


def test_sky130_spice_agent_defers_without_real_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_spice_text": ".subckt ana vin vout vdd vss\n* scaffold only\n.ends ana\n",
    }

    with pytest.raises(RuntimeError, match="requires real transistor-level SPICE"):
        spice_agent.run_agent(state)

    assert state["analog_sky130_spice"]["status"] == "failed"
    assert state["analog_sky130_spice"]["reason"] == "real_transistor_level_spice_missing"


def test_sky130_spice_agent_materializes_real_device_spice(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = spice_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_module": "ana",
        "analog_spice_text": ".subckt ana vin vout vdd vss\nM1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n.ends ana\n",
    })

    assert state["analog_sky130_spice"]["status"] == "ready"
    text = open(state["analog_spice_path"], "r", encoding="utf-8").read()
    assert "sky130.lib.spice" in text
    assert "sky130_fd_pr__nfet_01v8" in text


def test_sky130_spice_agent_generates_from_analog_spec_when_netlist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(
        spice_agent,
        "complete_text",
        lambda *args, **kwargs: ".subckt ana vin vout avdd avss\nM1 vout vin avss avss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\nM2 vout vin avdd avdd sky130_fd_pr__pfet_01v8 W=2u L=0.15u\n.ends ana\n",
    )

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_spec": {
            "module_name": "ana",
            "ports": [
                {"name": "vin", "direction": "input", "width": 1},
                {"name": "vout", "direction": "output", "width": 1},
                {"name": "avdd", "direction": "inout", "width": 1},
                {"name": "avss", "direction": "inout", "width": 1},
            ],
        },
    }

    state = spice_agent.run_agent(state)

    assert state["analog_sky130_spice"]["status"] == "ready"
    assert state["analog_sky130_spice"]["source"] == "generated_by_sky130_spice_agent"
    assert state["analog_sky130_spice"]["generated"] is True
    text = open(state["analog_spice_path"], "r", encoding="utf-8").read()
    assert ".subckt ana vin vout avdd avss" in text
    assert "sky130_fd_pr__nfet_01v8" in text


def test_sky130_spice_agent_generates_from_macro_contract_when_spec_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(
        spice_agent,
        "complete_text",
        lambda *args, **kwargs: ".subckt temp_sensor_adc_model sample_req sensor_temp_celsius adc_code adc_valid avdd avss\nM1 adc_valid sample_req avss avss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\nM2 adc_valid sample_req avdd avdd sky130_fd_pr__pfet_01v8 W=2u L=0.15u\n.ends temp_sensor_adc_model\n",
    )

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_interface_contract": {
            "macro_name": "temp_sensor_adc_model",
            "ports": [
                {"name": "sample_req", "verilog_direction": "input", "width": 1},
                {"name": "sensor_temp_celsius", "verilog_direction": "input", "width": 16},
                {"name": "adc_code", "verilog_direction": "output", "width": 12},
                {"name": "adc_valid", "verilog_direction": "output", "width": 1},
                {"name": "avdd", "verilog_direction": "input", "width": 1},
                {"name": "avss", "verilog_direction": "input", "width": 1},
            ],
        },
    }

    state = spice_agent.run_agent(state)

    assert state["analog_sky130_spice"]["status"] == "ready"
    assert state["analog_sky130_spice"]["source"] == "generated_by_sky130_spice_agent"
    assert state["analog_sky130_spice"]["module"] == "temp_sensor_adc_model"


def test_sky130_spice_agent_fails_when_generator_returns_no_real_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(spice_agent, "complete_text", lambda *args, **kwargs: ".subckt ana vin vout\n* no devices\n.ends ana\n")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_spec": {
            "module_name": "ana",
            "ports": [{"name": "vin", "direction": "input"}, {"name": "vout", "direction": "output"}],
        },
    }

    with pytest.raises(RuntimeError, match="requires real transistor-level SPICE"):
        spice_agent.run_agent(state)

    assert state["analog_sky130_spice"]["status"] == "failed"


def test_sky130_spice_agent_rejects_x_lines_as_mos_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(
        spice_agent,
        "complete_text",
        lambda *args, **kwargs: ".subckt ana vin vout vdd vss\nX1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n.ends ana\n",
    )

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_spec": {
            "module_name": "ana",
            "ports": [{"name": "vin", "direction": "input"}, {"name": "vout", "direction": "output"}],
        },
    }

    with pytest.raises(RuntimeError, match="requires real transistor-level SPICE"):
        spice_agent.run_agent(state)

    assert state["analog_sky130_spice"]["status"] == "failed"


def test_gds_generation_uses_macro_contract_name_when_module_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gds_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_interface_contract": {"macro_name": "temp_sensor_adc_model"},
    }

    with pytest.raises(RuntimeError, match="requires a generated or provided"):
        gds_agent.run_agent(state)

    assert state["analog_gds_generation"]["status"] == "failed"
    assert state["analog_gds_generation"]["module"] == "temp_sensor_adc_model"


def test_gds_generation_uses_magic_docker_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gds_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(gds_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    pdk_root = tmp_path / "pdk"
    magic_dir = pdk_root / "sky130A" / "libs.tech" / "magic"
    magic_dir.mkdir(parents=True)
    (magic_dir / "sky130A.tech").write_text("tech\n", encoding="utf-8")
    (magic_dir / "sky130A.tcl").write_text("proc sky130::importspice {} {}\n", encoding="utf-8")
    spice = tmp_path / "ana.spice"
    spice.write_text(
        ".subckt ana vin vout vdd vss\n"
        "M1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n"
        "M2 vout vin vss vss sky130_fd_pr__nfet_01v8 W=0.24u L=0.12u\n"
        ".ends ana\n",
        encoding="utf-8",
    )

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        assert capability == "analog_magic_gds"
        assert cmd[0] == "/usr/bin/docker"
        assert gds_agent.OPENLANE_DOCKER_IMAGE in cmd
        assert "magic" in cmd
        assert "/pdk/sky130A/libs.tech/magic/sky130A.tech" in cmd
        assert "/work/magic_import_spice.tcl" in cmd
        assert f"{pdk_root}:/pdk" in cmd
        assert any(str(part).endswith(":/work") for part in cmd)
        tcl = (tmp_path / "analog" / "gds" / "magic_import_spice.tcl").read_text(encoding="utf-8")
        assert "source /pdk/sky130A/libs.tech/magic/sky130A.tcl" in tcl
        assert "magic::netlist_to_layout ana.sp sky130" in tcl
        assert "CHIPLOOP_FINAL_BOX=[box values]" in tcl
        assert "gds write ana.gds" in tcl
        staged_spice = (tmp_path / "analog" / "gds" / "ana.sp").read_text(encoding="utf-8")
        assert "W=1 L=0.15" in staged_spice
        assert "W=0.42 L=0.15" in staged_spice
        (tmp_path / "analog" / "gds" / "ana.gds").write_bytes(b"GDS")
        return SimpleNamespace(returncode=0, stdout="magic ok", stderr="")

    monkeypatch.setattr(gds_agent, "run_command", fake_run_command)

    state = gds_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "pdk_root_host": str(pdk_root),
    })

    assert state["analog_gds_generation"]["status"] == "generated"
    assert state["analog_gds_generation"]["tool_mode"] == "docker_magic"
    assert state["analog_gds_generation"]["backend"] == "magic"


def test_gds_generation_rejects_magic_placeholder_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(gds_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(gds_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    pdk_root = tmp_path / "pdk"
    magic_dir = pdk_root / "sky130A" / "libs.tech" / "magic"
    magic_dir.mkdir(parents=True)
    (magic_dir / "sky130A.tech").write_text("tech\n", encoding="utf-8")
    (magic_dir / "sky130A.tcl").write_text("proc sky130::importspice {} {}\n", encoding="utf-8")
    spice = tmp_path / "ana.spice"
    spice.write_text(
        ".subckt ana vin vout vdd vss\n"
        "M1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n"
        ".ends ana\n",
        encoding="utf-8",
    )

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        (tmp_path / "analog" / "gds" / "ana.gds").write_bytes(b"GDS")
        return SimpleNamespace(
            returncode=0,
            stdout="CHIPLOOP_FINAL_BOX=0 0 0 0\n",
            stderr="",
        )

    monkeypatch.setattr(gds_agent, "run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="magic_zero_area_layout"):
        gds_agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "analog_physical_mode": "generate_sky130_gds",
            "analog_pdk": "sky130A",
            "analog_macro_module": "ana",
            "analog_spice_path": str(spice),
            "pdk_root_host": str(pdk_root),
        })

def test_gds_generation_uses_align_docker_when_host_align_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gds_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(gds_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    spice = tmp_path / "ana.spice"
    spice.write_text(
        ".subckt ana vin vout vdd vss\n"
        "M1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n"
        ".ends ana\n",
        encoding="utf-8",
    )

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        assert cmd[0] == "/usr/bin/docker"
        assert gds_agent.ALIGN_DOCKER_IMAGE in cmd
        assert any(str(part).endswith(":/pdk") for part in cmd)
        assert "-e" in cmd
        assert "PDK=sky130A" in cmd
        assert "PDK_ROOT=/pdk" in cmd
        assert "sh" in cmd
        script = cmd[-1]
        assert "set -eu" in script
        assert "pipefail" not in script
        assert 'PY_BIN="$(command -v python3 || command -v python)"' in script
        assert '${PY_BIN} - <<' in script
        assert "schematic2layout.py /work" in script
        assert 'ALIGN_PDK_DIR=${PDK_DIR}' in script
        assert "-p \"${PDK_DIR}\"" in script
        assert "Path('/pdk') / variant" in script
        assert "primitive.py" in script
        assert "rglob('primitive.py')" in script
        assert "-f ana.sp" in script
        assert "-s ana" in script
        assert "-c" not in cmd
        (tmp_path / "analog" / "gds" / "ana.gds").write_bytes(b"GDS")
        return SimpleNamespace(returncode=0, stdout="align ok", stderr="")

    monkeypatch.setattr(gds_agent, "run_command", fake_run_command)

    state = gds_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "analog_gds_backend": "align",
    })

    assert state["analog_gds_generation"]["status"] == "generated"
    assert state["analog_gds_generation"]["tool_mode"] == "docker"


def test_gds_generation_mounts_dedicated_align_pdk_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(gds_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(gds_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    spice = tmp_path / "ana.spice"
    spice.write_text(
        ".subckt ana vin vout vdd vss\n"
        "M1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n"
        ".ends ana\n",
        encoding="utf-8",
    )
    align_pdk = tmp_path / "align_sky130"
    align_pdk.mkdir()
    (align_pdk / "primitive.py").write_text("# align pdk\n", encoding="utf-8")

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        assert f"{align_pdk}:/align_pdk" in cmd
        assert "Path('/align_pdk')" in cmd[-1]
        (tmp_path / "analog" / "gds" / "ana.gds").write_bytes(b"GDS")
        return SimpleNamespace(returncode=0, stdout="align ok", stderr="")

    monkeypatch.setattr(gds_agent, "run_command", fake_run_command)

    state = gds_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "analog_gds_backend": "align",
        "align_pdk_host": str(align_pdk),
    })

    assert state["analog_gds_generation"]["align_pdk_host"] == str(align_pdk)
    assert state["analog_gds_generation"]["status"] == "generated"


def test_physical_package_exports_blackbox_when_not_generating_gds(tmp_path, monkeypatch):
    monkeypatch.setattr(package_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    lib.write_text("library (ana) {}\n", encoding="utf-8")

    state = package_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "blackbox",
        "analog_macro_module": "ana",
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
    })

    package = state["analog_physical_collateral_package"]
    assert package["blackbox_for_drc_lvs"] is True
    assert package["blackbox_reason"] == "analog_macro_gds_missing"
    assert state["digital"]["macro_blackbox_mode"] == "lef_lib_no_gds"


def test_physical_package_fails_in_generate_mode_when_gds_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(package_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    lib.write_text("library (ana) {}\n", encoding="utf-8")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
    }

    with pytest.raises(RuntimeError, match="missing spice, gds"):
        package_agent.run_agent(state)

    assert state["analog_physical_collateral_package"]["status"] == "failed"


def test_consistency_agent_detects_pin_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(consistency_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    spice = tmp_path / "ana.spice"
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    spice.write_text(".subckt ana vin vout vdd vss\n.ends ana\n", encoding="utf-8")
    lef.write_text("MACRO ana\n  PIN vin\n  END vin\nEND ana\n", encoding="utf-8")
    lib.write_text("library (ana) { cell (ana) { pin (vin) {} } }\n", encoding="utf-8")

    state = consistency_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
    })

    assert state["analog_collateral_consistency"]["status"] == "issues"
    assert any(issue.startswith("spice_pins_missing_in_lef") for issue in state["analog_collateral_consistency"]["issues"])


def test_consistency_agent_fails_in_generate_mode_on_pin_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(consistency_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    spice = tmp_path / "ana.spice"
    lef = tmp_path / "ana.lef"
    spice.write_text(".subckt ana vin vout vdd vss\n.ends ana\n", encoding="utf-8")
    lef.write_text("MACRO ana\n  PIN vin\n  END vin\nEND ana\n", encoding="utf-8")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "analog_macro_lef": str(lef),
    }

    with pytest.raises(RuntimeError, match="spice_pins_missing_in_lef"):
        consistency_agent.run_agent(state)

    assert state["analog_collateral_consistency"]["status"] == "issues"


def test_consistency_agent_accepts_bus_bits_and_warns_for_abstract_lib(tmp_path, monkeypatch):
    monkeypatch.setattr(consistency_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    spice = tmp_path / "ana.spice"
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    spice.write_text(".subckt ana adc_code adc_code[0] adc_code[1] sample_req\n.ends ana\n", encoding="utf-8")
    lef.write_text(
        "MACRO ana\n"
        "  PIN adc_code[0]\n  END adc_code[0]\n"
        "  PIN adc_code[1]\n  END adc_code[1]\n"
        "  PIN sample_req\n  END sample_req\n"
        "END ana\n",
        encoding="utf-8",
    )
    lib.write_text("library (ana) { cell (ana) { pin (sample_req) {} } }\n", encoding="utf-8")

    state = consistency_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
        "analog_liberty_characterization": {"status": "validated", "generated_liberty": False},
    })

    report = state["analog_collateral_consistency"]
    assert report["status"] == "warnings"
    assert report["issues"] == []
    assert any(w.startswith("lef_pins_missing_in_lib") for w in report["warnings"])


def test_lef_extraction_fails_without_gds(tmp_path, monkeypatch):
    monkeypatch.setattr(lef_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
    }

    with pytest.raises(RuntimeError, match="analog_macro_gds_missing"):
        lef_agent.run_agent(state)

    assert state["analog_lef_extraction"]["status"] == "failed"
    assert state["analog_lef_extraction"]["reason"] == "analog_macro_gds_missing"


def test_lef_extraction_uses_openlane_docker_when_host_magic_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(lef_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(lef_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    gds = tmp_path / "ana.gds"
    gds.write_bytes(b"GDS")

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        assert cmd[0] == "/usr/bin/docker"
        assert lef_agent.OPENLANE_DOCKER_IMAGE in cmd
        assert "magic" in cmd
        (tmp_path / "analog" / "lef_extract" / "ana.lef").write_text(
            "MACRO ana\n  PIN vin\n  END vin\nEND ana\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="magic ok", stderr="")

    monkeypatch.setattr(lef_agent, "run_command", fake_run_command)

    state = lef_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_macro_gds": str(gds),
    })

    assert state["analog_lef_extraction"]["status"] == "extracted"
    assert state["analog_lef_extraction"]["tool_mode"] == "docker"


def test_lef_extraction_rejects_placeholder_without_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(lef_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(lef_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    gds = tmp_path / "ana.gds"
    gds.write_bytes(b"GDS")

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        (tmp_path / "analog" / "lef_extract" / "ana.lef").write_text(
            "MACRO ana\n  SIZE 1.000 BY 1.000 ;\nEND ana\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="magic ok", stderr="")

    monkeypatch.setattr(lef_agent, "run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="lef_missing_pins|lef_placeholder_geometry"):
        lef_agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "analog_physical_mode": "generate_sky130_gds",
            "analog_macro_module": "ana",
            "analog_macro_gds": str(gds),
        })


def test_lef_extraction_pinizes_magic_lef_from_prior_macro_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(lef_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(lef_agent.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    gds = tmp_path / "ana.gds"
    prior_lef = tmp_path / "prior.lef"
    gds.write_bytes(b"GDS")
    prior_lef.write_text(
        "MACRO ana\n"
        "  PIN avdd\n    DIRECTION INOUT ;\n  END avdd\n"
        "  PIN adc_valid\n    DIRECTION OUTPUT ;\n  END adc_valid\n"
        "END ana\n",
        encoding="utf-8",
    )

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None):
        (tmp_path / "analog" / "lef_extract" / "ana.lef").write_text(
            "MACRO ana\n  SIZE 10.000 BY 20.000 ;\nEND ana\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="magic ok", stderr="")

    monkeypatch.setattr(lef_agent, "run_command", fake_run_command)

    state = lef_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_macro_gds": str(gds),
        "analog_macro_lef": str(prior_lef),
    })

    text = (tmp_path / "analog" / "lef_extract" / "ana.lef").read_text(encoding="utf-8")
    assert "PIN adc_valid" in text
    assert "PIN avdd" in text
    assert "SIZE 100.000 BY 100.000" in text
    assert "LAYER met2" in text
    assert "LAYER met4" in text
    assert "SHAPE ABUTMENT" in text
    avdd_block = re.search(r"PIN avdd\b(.*?)END avdd", text, flags=re.DOTALL)
    assert avdd_block
    block = avdd_block.group(1)
    assert block.index("SHAPE ABUTMENT") < block.index("PORT") < block.index("LAYER met4")
    for rect in re.findall(r"\bRECT\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*;", text):
        for value in rect:
            assert round(float(value) * 1000) % 5 == 0
    assert state["analog_lef_extraction"]["status"] == "extracted"
    assert state["analog_lef_extraction"]["pinized_from_macro_interface"] is True
    assert state["analog_lef_extraction"]["pin_count"] == 2


def test_liberty_characterization_fails_without_spice(tmp_path, monkeypatch):
    monkeypatch.setattr(lib_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
    }

    with pytest.raises(RuntimeError, match="analog_spice_missing"):
        lib_agent.run_agent(state)

    assert state["analog_liberty_characterization"]["status"] == "failed"
    assert state["analog_liberty_characterization"]["reason"] == "analog_spice_missing"


def test_liberty_characterization_sets_pdk_root_and_uploads_ngspice_log(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(lib_agent, "save_text_artifact_and_record", lambda wf, agent, folder, name, text: saved.setdefault(name, text) or "local")
    monkeypatch.setattr(lib_agent.shutil, "which", lambda name: "/usr/bin/ngspice" if name == "ngspice" else None)
    pdk_root = tmp_path / "pdk"
    pdk_root.mkdir()
    spice = tmp_path / "ana.spice"
    spice.write_text(".subckt ana vin vout vdd vss\n.ends ana\n", encoding="utf-8")

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None, env=None):
        assert env == {"PDK_ROOT": str(pdk_root)}
        assert cmd == ["/usr/bin/ngspice", "-b", str(tmp_path / "analog" / "lib_char" / "characterize_ngspice.sp")]
        return SimpleNamespace(returncode=1, stdout="ngspice failed detail", stderr="")

    monkeypatch.setattr(lib_agent, "run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="ngspice failed detail"):
        lib_agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "analog_physical_mode": "generate_sky130_gds",
            "analog_macro_module": "ana",
            "analog_spice_path": str(spice),
            "pdk_root_host": str(pdk_root),
        })

    assert "ngspice_characterization.log" in saved


def test_liberty_characterization_removes_duplicate_sky130_include(tmp_path, monkeypatch):
    monkeypatch.setattr(lib_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(lib_agent.shutil, "which", lambda name: "/usr/bin/ngspice" if name == "ngspice" else None)
    pdk_root = tmp_path / "pdk"
    pdk_root.mkdir()
    spice = tmp_path / "ana.spice"
    spice.write_text(
        '.include "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"\n'
        '.lib "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice" tt\n'
        ".subckt ana vin vout vdd vss\n"
        "M1 vout vin vss vss sky130_fd_pr__nfet_01v8 W=0.84u L=0.15u\n"
        ".ends ana\n",
        encoding="utf-8",
    )

    def fake_run_command(state, capability, cmd, cwd=None, timeout_sec=None, env=None):
        staged = tmp_path / "analog" / "lib_char" / "ana.spice"
        text = staged.read_text(encoding="utf-8")
        assert ".include" not in text
        assert '.lib "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice" tt' in text
        return SimpleNamespace(returncode=0, stdout="ngspice ok", stderr="")

    monkeypatch.setattr(lib_agent, "run_command", fake_run_command)

    state = lib_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_spice_path": str(spice),
        "pdk_root_host": str(pdk_root),
    })

    assert state["analog_liberty_characterization"]["status"] == "validated"
    assert state["analog_liberty_characterization"]["reason"] == "liberty_not_produced"
    assert state["analog_liberty_characterization"]["generated_liberty"] is False


def test_liberty_characterization_contract_spec_collapses_bus_bit_pins():
    spec = lib_agent._contract_spec(
        {
            "analog_macro_interface_contract": {
                "macro_name": "generic_macro",
                "ports": [
                    {"name": "data_out", "verilog_direction": "output", "width": "[11:0]"},
                    {"name": "data_out[0]", "lef_direction": "OUTPUT", "width": "unknown"},
                    {"name": "data_out[11]", "lef_direction": "OUTPUT", "width": "unknown"},
                    {"name": "sample_in", "verilog_direction": "input", "width": "[15:0]"},
                    {"name": "sample_in[15]", "lef_direction": "INPUT", "width": "unknown"},
                    {"name": "avdd", "verilog_direction": "inout", "width": "1"},
                    {"name": "avss", "verilog_direction": "inout", "width": "1"},
                ],
            }
        },
        "generic_macro",
    )
    ports = {p["name"]: p for p in spec["ports"]}

    assert ports["data_out"]["direction"] == "output"
    assert ports["data_out"]["width"] == 12
    assert ports["sample_in"]["direction"] == "input"
    assert ports["sample_in"]["width"] == 16
    assert "data_out[0]" not in ports

    lib = abstract_agent._build_lib_stub(spec)
    assert "bus (data_out)" in lib
    assert 'pin ("data_out[11]")' in lib
    assert "pin (data_out[11])" not in lib


def test_macro_interface_contract_and_validation_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(contract_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(validation_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    model = tmp_path / "ana.sv"
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    model.write_text("module ana(input vin, output vout, inout avdd, inout avss); endmodule\n", encoding="utf-8")
    lef.write_text(
        "MACRO ana\n"
        "  PIN vin\n    DIRECTION INPUT ;\n  END vin\n"
        "  PIN vout\n    DIRECTION OUTPUT ;\n  END vout\n"
        "  PIN avdd\n    DIRECTION INOUT ;\n  END avdd\n"
        "  PIN avss\n    DIRECTION INOUT ;\n  END avss\n"
        "END ana\n",
        encoding="utf-8",
    )
    lib.write_text(
        "library (ana) { cell (ana) { pin (vin) { direction : input; } pin (vout) { direction : output; } } }\n",
        encoding="utf-8",
    )

    state = contract_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_macro_module": "ana",
        "analog_model_path": str(model),
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
    })
    state = validation_agent.run_agent(state)

    assert state["analog_macro_interface_contract"]["macro_name"] == "ana"
    assert state["analog_macro_interface_validation"]["status"] == "pass"


def test_macro_interface_validation_resolves_imported_pd_collateral(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    imported_rtl = tmp_path / "digital" / "system" / "imported_rtl"
    imported_macros = tmp_path / "system" / "imported_macros"
    imported_rtl.mkdir(parents=True)
    imported_macros.mkdir(parents=True)
    model = imported_rtl / "temp_sensor_adc_model.v"
    lef = imported_macros / "temp_sensor_adc_model.lef"
    lib = imported_macros / "temp_sensor_adc_model.lib"
    model.write_text(
        "module temp_sensor_adc_model(\n"
        "  input [15:0] sensor_temp_celsius,\n"
        "  input avdd,\n"
        "  input avss,\n"
        "  input sample_req,\n"
        "  output [11:0] adc_code,\n"
        "  output adc_valid\n"
        "); endmodule\n",
        encoding="utf-8",
    )
    lef.write_text(
        "MACRO temp_sensor_adc_model\n"
        "  PIN VPWR\n    DIRECTION INOUT ;\n  END VPWR\n"
        "  PIN VGND\n    DIRECTION INOUT ;\n  END VGND\n"
        "  PIN adc_code[0]\n    DIRECTION OUTPUT ;\n  END adc_code[0]\n"
        "  PIN adc_valid\n    DIRECTION OUTPUT ;\n  END adc_valid\n"
        "  PIN sample_req\n    DIRECTION INPUT ;\n  END sample_req\n"
        "END temp_sensor_adc_model\n",
        encoding="utf-8",
    )
    lib.write_text("library (ana) { cell (temp_sensor_adc_model) { pin (adc_valid) { direction : output; } } }\n", encoding="utf-8")
    contract = {
        "macro_name": "temp_sensor_adc_model",
        "ports": [
            {"name": "sensor_temp_celsius", "role": "signal", "verilog_direction": "input", "lef_direction": "missing", "lib_direction": "missing"},
            {"name": "adc_code", "role": "signal", "verilog_direction": "output", "lef_direction": "OUTPUT", "lib_direction": "missing"},
            {"name": "adc_code[0]", "role": "signal", "verilog_direction": "output", "lef_direction": "OUTPUT", "lib_direction": "missing"},
            {"name": "adc_valid", "role": "signal", "verilog_direction": "output", "lef_direction": "OUTPUT", "lib_direction": "missing"},
            {"name": "avdd", "role": "power", "verilog_direction": "input", "lef_direction": "INOUT", "lib_direction": "missing"},
            {"name": "VPWR", "role": "power", "verilog_direction": "input", "lef_direction": "INOUT", "lib_direction": "missing"},
            {"name": "avss", "role": "ground", "verilog_direction": "input", "lef_direction": "INOUT", "lib_direction": "missing"},
            {"name": "VGND", "role": "ground", "verilog_direction": "input", "lef_direction": "INOUT", "lib_direction": "missing"},
        ],
    }

    state = validation_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_macro_interface_contract": contract,
        "digital": {"macro_lefs": [str(lef)], "macro_libs": [str(lib)]},
    })

    report = state["analog_macro_interface_validation"]
    assert report["status"] == "pass"
    assert report["observed"]["verilog_module"] == "temp_sensor_adc_model"
    assert report["resolved_sources"]["model"] == str(model)
    assert state["analog_macro_lef"] == str(lef)


def test_macro_interface_validation_fails_on_direction_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    model = tmp_path / "ana.sv"
    lef = tmp_path / "ana.lef"
    model.write_text("module ana(input vin); endmodule\n", encoding="utf-8")
    lef.write_text("MACRO ana\n  PIN vin\n    DIRECTION OUTPUT ;\n  END vin\nEND ana\n", encoding="utf-8")
    contract = {
        "macro_name": "ana",
        "ports": [{"name": "vin", "verilog_direction": "input", "lef_direction": "INPUT", "lib_direction": "missing"}],
    }

    state = validation_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_interface_fail_fast": "false",
        "analog_macro_interface_contract": contract,
        "analog_model_path": str(model),
        "analog_macro_lef": str(lef),
    })

    assert state["analog_macro_interface_validation"]["status"] == "fail"
    assert any(issue["type"] == "lef_direction_mismatch" for issue in state["analog_macro_interface_validation"]["issues"])
