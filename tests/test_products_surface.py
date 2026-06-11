from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = BACKEND_ROOT / "main.py"
MIGRATION_SQL = (
    BACKEND_ROOT / "supabase"
    / "migrations"
    / "phase_zzzzzzzzzzzzzzz_products_reference_journeys.sql"
)


def test_reference_journey_defaults_cover_current_manual_journeys():
    source = MAIN_PY.read_text(encoding="utf-8")

    assert '"slug": "pwm-fan-controller"' in source
    assert '"slug": "uart-packet-engine"' in source
    assert '"slug": "image-dma-pipeline"' in source
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
def test_product_api_routes_are_registered():
    source = MAIN_PY.read_text(encoding="utf-8")

    assert '@app.get("/products/reference-journeys")' in source
    assert '@app.get("/products")' in source
    assert '@app.post("/products")' in source
    assert '@app.patch("/products/{product_id}")' in source
