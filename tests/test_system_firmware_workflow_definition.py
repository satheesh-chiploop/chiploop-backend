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


def test_system_migration_is_full_chain_not_downstream_only():
    sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "'System_RTL'" in sql
    assert "'System_Sim'" in sql
    assert sql.index("'System_RTL'") < sql.index("'System_Sim'") < sql.index("'System_Firmware'")
    assert sql.index("'Digital Spec Agent'") < sql.index("'System RTL Handoff Package Agent'")
    assert sql.index("'Digital Register Map Agent'") < sql.index("'Digital RTL Agent'")
    assert sql.index("'System RTL Handoff Package Agent'") < sql.index("'System CoSim Ingest Agent'")
    assert sql.index("'System CoSim Ingest Agent'") < sql.index("'System Testbench Generator Agent'")
    assert sql.index("'System RTL Handoff Package Agent'") < sql.index("'Embedded Digital RTL Handoff Ingest Agent'")
    assert "'System RTL Evidence Dashboard Agent'" in sql
