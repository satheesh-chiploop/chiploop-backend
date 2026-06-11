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
