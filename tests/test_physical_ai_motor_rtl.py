from physical_ai.fixed_point import analyze_fixed_point
from physical_ai.pmsm_equations import simulate_pmsm
from physical_ai.rtl_motor import generate_motor_rtl
from agents.fpga.fpga_common import resolve_rtl_sources


def test_motor_rtl_compiles_and_smoke_passes(tmp_path):
    simulation = simulate_pmsm({}, tmp_path)
    fixed = analyze_fixed_point(simulation["timeseries"], {}, tmp_path)
    rtl = generate_motor_rtl({}, tmp_path, fixed["rtl_contract"])
    assert rtl["manifest"]["status"] == "smoke_verified"
    assert rtl["manifest"]["verification"]["compiled"] is True
    assert rtl["manifest"]["verification"]["smoke_passed"] is True
    assert "MOTOR_RTL_SMOKE_PASS" in rtl["manifest"]["verification"]["run_stdout"]
    assert len(rtl["manifest"]["sources"]) == 9
    assert rtl["manifest"]["firmware_top_module"] == "motor_control_mmio_top"
    for path in rtl["files"].values():
        assert open(path, encoding="utf-8").read()


def test_fpga_handoff_imports_physical_ai_run_rtl_and_excludes_testbench(tmp_path):
    source_root = tmp_path / "physical-run"
    source_root.mkdir()
    (source_root / "motor_control_top.sv").write_text("module motor_control_top; endmodule\n", encoding="utf-8")
    (source_root / "tb_motor_control.sv").write_text("module tb_motor_control; endmodule\n", encoding="utf-8")

    class Response:
        def __init__(self, data):
            self.data = data

    class Query:
        def __init__(self, table):
            self.table = table
        def select(self, *_args, **_kwargs): return self
        def eq(self, *_args, **_kwargs): return self
        def single(self): return self
        def order(self, *_args, **_kwargs): return self
        def execute(self):
            return Response([{"artifacts_path": str(source_root)}] if self.table == "runs" else {"artifacts": {}})

    class Bucket:
        def list(self, *_args, **_kwargs): return []
        def download(self, *_args, **_kwargs): return None

    class Storage:
        def from_(self, *_args, **_kwargs): return Bucket()

    class Client:
        storage = Storage()
        def table(self, name): return Query(name)

    sources = resolve_rtl_sources({
        "workflow_id": "child",
        "workflow_dir": str(tmp_path / "child"),
        "artifact_dir": str(tmp_path / "child"),
        "from_workflow_id": "physical-parent",
        "rtl_source_mode": "from_arch2rtl",
        "supabase_client": Client(),
    })
    assert any(path.endswith("motor_control_top.sv") for path in sources)
    assert not any(path.endswith("tb_motor_control.sv") for path in sources)
