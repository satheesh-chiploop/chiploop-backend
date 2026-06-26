from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = BACKEND_ROOT / "main.py"
MIGRATION_SQL = (
    BACKEND_ROOT / "supabase"
    / "migrations"
    / "phase_zzzzzzzzzzzzzzz_products_reference_journeys.sql"
)
RUN_MIGRATION_SQL = (
    BACKEND_ROOT / "supabase"
    / "migrations"
    / "phase_zzzzzzzzzzzzzzzz_product_run_orchestration.sql"
)
SCHEMA_MIGRATION_SQL = (
    BACKEND_ROOT / "supabase"
    / "migrations"
    / "phase_zzzzzzzzzzzzzzzzz_product_stage_schemas.sql"
)


def test_reference_journey_defaults_cover_current_manual_journeys():
    source = MAIN_PY.read_text(encoding="utf-8")

    assert '"slug": "pwm-fan-controller"' in source
    assert '"slug": "uart-packet-engine"' in source
    assert '"slug": "image-dma-pipeline"' in source
    assert '"slug": "soft-digital-ip-product"' in source
    assert '"name": "Soft Digital IP Product"' in source
    assert '"slug": "temperature-monitor-soc"' in source

    tempmon_block = source[source.index('"slug": "temperature-monitor-soc"'):]
    assert '"app": "System_RTL"' in tempmon_block
    assert '"app": "System_DQA"' in tempmon_block
    assert '"app": "System_Sim"' in tempmon_block
    assert '"app": "System_Firmware"' in tempmon_block
    assert '"app": "System_Product_App_Builder"' in tempmon_block


def test_product_migration_uses_separate_product_tables_not_workflows_definition_column():
    sql = MIGRATION_SQL.read_text(encoding="utf-8")

    assert "create table if not exists public.products" in sql
    assert "create table if not exists public.product_reference_journeys" in sql
    assert "insert into public.workflows" not in sql
    assert "public.workflows" not in sql
    assert "definition jsonb" in sql
    assert "stage_config jsonb" in sql
    assert "'soft-digital-ip-product'" in sql
    assert "'Soft Digital IP Product'" in sql
    assert "Users can delete own products" in sql
    run_sql = RUN_MIGRATION_SQL.read_text(encoding="utf-8")
    assert "create table if not exists public.product_runs" in run_sql
    assert "create table if not exists public.product_stage_runs" in run_sql
    assert "workflow_id uuid null" in run_sql
    assert "logs text not null default ''" in run_sql
    assert "add column if not exists logs" in run_sql
    assert "Users can delete own product runs" in run_sql
    assert "Users can delete own product stage runs" in run_sql

    schema_sql = SCHEMA_MIGRATION_SQL.read_text(encoding="utf-8")
    assert "create table if not exists public.product_stage_schemas" in schema_sql
    assert "insert into public.product_stage_schemas" in schema_sql
    assert "public.workflows" not in schema_sql
    assert "'System_RTL'" in schema_sql
    assert '"digital_spec"' in schema_sql
    assert '"required":true' in schema_sql
    assert '"run_spec2rtl_check"' in schema_sql
    assert '"formal_tool"' in schema_sql
    assert '"type":"select","defaultValue":"none","options":[{"value":"none","label":"Disabled"},{"value":"symbiyosys"' in schema_sql
    assert '"formal_solver","label":"Formal solver","type":"select","defaultValue":"z3","options":[{"value":"z3","label":"Z3"},{"value":"boolector","label":"Boolector"}]' in schema_sql
    assert '"golden_model_tool"' in schema_sql
    assert '"code_coverage_tool","label":"Code coverage tool","type":"select"' in schema_sql
    assert '"failure_debug_generate_vcd"' in schema_sql
    assert "'Digital_DQA'" in schema_sql
    assert '"run_synthesis_readiness"' in schema_sql
    assert '"analog_physical_mode"' in schema_sql
    assert '"run_lef_lib_consistency"' in schema_sql


def test_product_api_routes_are_registered():
    source = MAIN_PY.read_text(encoding="utf-8")
    frontend_source = (BACKEND_ROOT.parent / "frontend" / "app" / "products" / "[productId]" / "page.tsx").read_text(encoding="utf-8")

    assert '@app.get("/products/reference-journeys")' in source
    assert '@app.get("/products/stage-schemas")' in source
    assert '@app.get("/products")' in source
    assert '@app.get("/products/{product_id}")' in source
    assert '@app.post("/products")' in source
    assert '@app.patch("/products/{product_id}")' in source
    assert '@app.delete("/products/{product_id}")' in source
    assert '@app.post("/products/{product_id}/run")' in source
    assert '@app.get("/products/{product_id}/runs")' in source
    assert '@app.get("/products/{product_id}/runs/{product_run_id}")' in source
    assert '@app.delete("/products/{product_id}/runs/{product_run_id}")' in source
    assert '@app.post("/products/{product_id}/runs/{product_run_id}/cancel")' in source
    assert "payload.start_stage" in source
    assert "Start stage" in source
    assert "_product_run_cancelled" in source
    assert "_update_product_status" in source
    assert "resume_product_run_id" in source
    assert "_seed_product_upstream_from_prior_run" in source
    assert "_product_upstream_key_for_app" in source
    assert "_append_product_run_log" in source
    assert "run_config_json" in source
    assert 'shared_state["run_config"]' in source
    assert '"workflow_config_schema": workflow_config_schema' in source
    assert '"logs": "Product run queued."' in source
    assert "PRODUCT_STAGE_SCHEMAS" in source
    assert 'supabase.table("product_stage_schemas")' in source
    assert '"required": True' in source
    assert '"insert_mbist"' in source
    assert '"mbist_algorithm"' in source
    assert '"Digital_Arch2Synthesis": "Digital_Arch2Synthesis"' in source
    assert '"Digital_Arch2Tapeout": "Digital_Arch2Tapeout"' in source
    assert 'if stage.get("optional") and "enabled" not in stage' in source
    assert '"run_spec2rtl_check"' in source
    assert '"formal_tool"' in source
    assert '"golden_model_tool"' in source
    assert '"formal_solver", "label": "Formal solver", "type": "select"' in source
    assert '"code_coverage_tool", "label": "Code coverage tool", "type": "select"' in source
    assert '"app_type", "label": "App type", "type": "select"' in source
    assert '"build_system", "label": "Build system", "type": "select"' in source
    assert '"System_RTL": "System_RTL"' in source
    assert '"System_DQA": "System_DQA"' in source
    assert '"System_Sim": "System_Sim"' in source
    assert '"System_Synthesis": "System_Synthesis"' in source
    assert '"System_Firmware": "System_Firmware"' in source
    assert '"System_PD": "System_PD"' in source
    assert '"analog_spice_text", "label": "Provided analog SPICE/netlist"' in source
    assert '"run_spec2rtl_check", "label": "Run Spec2RTL compliance check"' in source
    assert '"enable_scan_dft", "label": "Enable scan/DFT intent"' in source
    assert '"system_sim_num_iters", "label": "Iteration budget"' in source
    assert 'deleted_product_id' in source
    assert '"dashboard_url"' in source
    assert '"download_url"' in source
    assert "max_stages: Optional[int] = None" in source
    assert "min(int(max_stages or 8), 8)" not in source
    assert "max_stages: 8" not in frontend_source
    assert "deleted_product_run_id" in source
    assert "deleteRunHistory" in frontend_source
    assert "apiDelete<{ status: string; deleted_product_run_id: string }>" in frontend_source


def test_product_stage_schema_seed_has_latest_digital_controls():
    source = SCHEMA_MIGRATION_SQL.read_text(encoding="utf-8")
    assert "Digital_Arch2Tapeout" in source
    assert "insert_mbist" in source
    assert "mbist_algorithm" in source
    assert "run_synthesis_closure_loop" in source
    assert '"random_vs_directed","label":"Random vs directed","type":"select"' in source
    assert '"rerun_mode","label":"Rerun mode","type":"select"' in source
    assert '"firmware_language","label":"Firmware language","type":"select"' in source
    assert '"sdk_style","label":"SDK style","type":"select"' in source
    assert '"target_runtime","label":"Target runtime","type":"select"' in source
    assert "'System_Synthesis'" in source
    assert '"analog_spice_text","label":"Provided analog SPICE/netlist"' in source
    assert '"effort","label":"Implementation effort","type":"select"' in source
    assert '"system_sim_num_iters","label":"Iteration budget","type":"number"' in source
    assert '"failure_debug_auto_apply_rtl","label":"Auto-apply RTL fixes"' in source
