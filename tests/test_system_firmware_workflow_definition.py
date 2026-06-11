from pathlib import Path


MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"
MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "phase_zzzzzzzzzzzzz_system_firmware_package_handoff_alignment.sql"
)


def _between(text: str, start: str, end: str) -> str:
    start_idx = text.index(start)
    end_idx = text.index(end, start_idx)
    return text[start_idx:end_idx]


def test_system_firmware_default_starts_with_system_rtl_generation():
    text = MAIN_PY.read_text(encoding="utf-8")
    block = _between(text, "SYSTEM_FIRMWARE_DEFINITION =", "SYSTEM_SIM_DEFINITION =")
    rtl_block = _between(text, "SYSTEM_RTL_AGENT_SEQUENCE =", "SYSTEM_RTL_DEFINITION =")
    downstream_block = _between(text, "FIRMWARE_DOWNSTREAM_DEFINITION =", "EMBEDDED_RUN_DEFINITION =")

    assert rtl_block.index('"Digital Spec Agent"') < rtl_block.index('"System RTL Handoff Package Agent"')
    assert rtl_block.index('"Digital Register Map Agent"') < rtl_block.index('"Digital RTL Agent"')
    assert rtl_block.index('"Analog Behavioral Model Agent"') < rtl_block.index('"Analog Abstract Views Agent"')
    assert rtl_block.index('"Analog Abstract Views Agent"') < rtl_block.index('"System RTL Handoff Package Agent"')
    assert block.index("*SYSTEM_RTL_AGENT_SEQUENCE") < block.index("*FIRMWARE_DOWNSTREAM_DEFINITION")
    assert '"Embedded Digital RTL Handoff Ingest Agent"' in downstream_block
    assert '"System_Firmware": SYSTEM_FIRMWARE_DEFINITION' in text
    assert '"System_Firmware",' in _between(text, "LOCAL_RUNTIME_WORKFLOW_OVERRIDES =", "# Dynamically load")


def test_system_rtl_default_publishes_register_map_before_rtl():
    text = MAIN_PY.read_text(encoding="utf-8")
    block = _between(text, "SYSTEM_RTL_AGENT_SEQUENCE =", "SYSTEM_RTL_DEFINITION =")

    assert block.index('"Digital Register Map Agent"') < block.index('"Digital RTL Agent"')
    assert block.index('"Digital Register Map Agent"') < block.index('"System RTL Handoff Package Agent"')


def test_system_sim_default_builds_rtl_before_downstream_sim():
    text = MAIN_PY.read_text(encoding="utf-8")
    block = _between(text, "SYSTEM_SIM_DEFINITION =", "SYSTEM_SIM_CLOSURE_DEFINITION =")
    downstream_block = _between(text, "SYSTEM_SIM_DOWNSTREAM_DEFINITION =", "SYSTEM_SIM_DEFINITION =")

    assert "*SYSTEM_RTL_AGENT_SEQUENCE" in block
    assert "*SYSTEM_SIM_DOWNSTREAM_DEFINITION" in block
    assert '"System CoSim Ingest Agent"' in downstream_block
    assert downstream_block.index('"System CoSim Ingest Agent"') < downstream_block.index('"System Testbench Generator Agent"')


def test_system_dqa_exists_and_reuses_system_rtl_sequence():
    text = MAIN_PY.read_text(encoding="utf-8")
    block = _between(text, "SYSTEM_DQA_DEFINITION =", "SYSTEM_SYNTHESIS_DEFINITION =")

    assert "*SYSTEM_RTL_AGENT_SEQUENCE" in block
    assert block.index("*SYSTEM_RTL_AGENT_SEQUENCE") < block.index('"Digital CDC Analysis Agent"')
    assert '"System_DQA": SYSTEM_DQA_DEFINITION' in text
    assert '"System_DQA",' in _between(text, "LOCAL_RUNTIME_WORKFLOW_OVERRIDES =", "# Dynamically load")
    assert '{"System_DQA", "System_Sim", "System_Firmware", "System_Synthesis", "System_PD"}' in text


def test_system_migration_is_full_chain_not_downstream_only():
    sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "'System_RTL'" in sql
    assert "'System_DQA'" in sql
    assert "'System_Sim'" in sql
    assert sql.index("'System_RTL'") < sql.index("'System_Sim'") < sql.index("'System_DQA'") < sql.index("'System_Synthesis'") < sql.index("'System_PD'") < sql.index("'System_Firmware'")
    assert sql.index("'Digital Spec Agent'") < sql.index("'System RTL Handoff Package Agent'")
    assert sql.index("'Digital Register Map Agent'") < sql.index("'Digital RTL Agent'")
    assert sql.index("'Analog Behavioral Model Agent'") < sql.index("'Analog Abstract Views Agent'")
    assert sql.index("'Analog Abstract Views Agent'") < sql.index("'System RTL Handoff Package Agent'")
    assert sql.index("'System RTL Handoff Package Agent'") < sql.index("'System CoSim Ingest Agent'")
    assert sql.index("'System CoSim Ingest Agent'") < sql.index("'System Testbench Generator Agent'")
    assert sql.index("'System RTL Handoff Package Agent'") < sql.index("'Embedded Digital RTL Handoff Ingest Agent'")
    assert "'System RTL Evidence Dashboard Agent'" in sql


def test_system_pd_has_optional_analog_gds_generation_path():
    text = MAIN_PY.read_text(encoding="utf-8")
    block = _between(text, "SYSTEM_PD_DEFINITION =", "SYSTEM_FIRMWARE_DEFINITION =")

    assert '"Analog Sky130 SPICE Netlist Agent"' in block
    assert '"Analog GDS Generation Agent"' in block
    assert '"Analog LEF Extraction Agent"' in block
    assert '"Analog Liberty Characterization Agent"' in block
    assert '"Analog Collateral Consistency Agent"' in block
    assert '"Analog Physical Collateral Package Agent"' in block
    assert block.index('"Digital MBIST Collateral Agent"') < block.index('"Analog Sky130 SPICE Netlist Agent"')
    assert block.index('"Analog GDS Generation Agent"') < block.index('"Analog LEF Extraction Agent"')
    assert block.index('"Analog Liberty Characterization Agent"') < block.index('"Analog Collateral Consistency Agent"')
    assert block.index('"Analog Physical Collateral Package Agent"') < block.index('"Digital STA PrePlace Agent"')
    assert "analog_physical_mode: Optional[str]" in text

    sql = MIGRATION_SQL.read_text(encoding="utf-8")
    assert "'Analog Sky130 SPICE Netlist Agent'" in sql
    assert sql.index("'Digital MBIST Collateral Agent'") < sql.index("'Analog Sky130 SPICE Netlist Agent'")
    assert sql.index("'Analog GDS Generation Agent'") < sql.index("'Analog LEF Extraction Agent'")
    assert sql.index("'Analog Liberty Characterization Agent'") < sql.index("'Analog Collateral Consistency Agent'")
    assert sql.index("'Analog Physical Collateral Package Agent'") < sql.index("'Digital STA PrePlace Agent'")
