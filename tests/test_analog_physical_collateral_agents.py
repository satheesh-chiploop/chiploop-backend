import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.analog import analog_sky130_spice_netlist_agent as spice_agent
from agents.analog import analog_physical_collateral_package_agent as package_agent
from agents.analog import analog_collateral_consistency_agent as consistency_agent
from agents.analog import analog_lef_extraction_agent as lef_agent
from agents.analog import analog_liberty_characterization_agent as lib_agent
from agents.analog import analog_macro_interface_contract_agent as contract_agent
from agents.analog import analog_macro_interface_validation_agent as validation_agent


def test_sky130_spice_agent_defers_without_real_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(spice_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = spice_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_pdk": "sky130A",
        "analog_spice_text": ".subckt ana vin vout vdd vss\n* scaffold only\n.ends ana\n",
    })

    assert state["analog_sky130_spice"]["status"] == "deferred"
    assert state["analog_sky130_spice"]["reason"] == "real_transistor_level_spice_missing"
    assert "analog_spice_path" not in state


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

    state = spice_agent.run_agent({
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
    })

    assert state["analog_sky130_spice"]["status"] == "ready"
    assert state["analog_sky130_spice"]["source"] == "generated_from_analog_spec"
    assert state["analog_sky130_spice"]["generated"] is True
    text = open(state["analog_spice_path"], "r", encoding="utf-8").read()
    assert ".subckt ana vin vout avdd avss" in text
    assert "sky130_fd_pr__nfet_01v8" in text
    assert "sky130_fd_pr__pfet_01v8" in text


def test_physical_package_exports_blackbox_when_gds_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(package_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    lib.write_text("library (ana) {}\n", encoding="utf-8")

    state = package_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
        "analog_macro_lef": str(lef),
        "analog_macro_lib": str(lib),
    })

    package = state["analog_physical_collateral_package"]
    assert package["blackbox_for_drc_lvs"] is True
    assert package["blackbox_reason"] == "analog_macro_gds_missing"
    assert state["digital"]["macro_blackbox_mode"] == "lef_lib_no_gds"


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


def test_lef_extraction_defers_without_gds(tmp_path, monkeypatch):
    monkeypatch.setattr(lef_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = lef_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
    })

    assert state["analog_lef_extraction"]["status"] == "deferred"
    assert state["analog_lef_extraction"]["reason"] == "analog_macro_gds_missing"


def test_liberty_characterization_defers_without_spice(tmp_path, monkeypatch):
    monkeypatch.setattr(lib_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    state = lib_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "analog_physical_mode": "generate_sky130_gds",
        "analog_macro_module": "ana",
    })

    assert state["analog_liberty_characterization"]["status"] == "deferred"
    assert state["analog_liberty_characterization"]["reason"] == "analog_spice_missing"


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
