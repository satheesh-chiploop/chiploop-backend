import json
from pathlib import Path

from agents.system import system_cosim_ingest_agent as ingest


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, row):
        self.row = row

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def single(self):
        return self

    def execute(self):
        return _Result(self.row)


class _Bucket:
    def __init__(self, payloads):
        self.payloads = payloads

    def download(self, path):
        if path not in self.payloads:
            raise FileNotFoundError(path)
        return self.payloads[path]

    def list(self, _folder):
        return []


class _Storage:
    def __init__(self, payloads):
        self.payloads = payloads

    def from_(self, _bucket):
        return _Bucket(self.payloads)


class _Supabase:
    def __init__(self, row, payloads):
        self.row = row
        self.storage = _Storage(payloads)

    def table(self, _name):
        return _Query(self.row)


def test_verified_simulation_bundle_restores_manifest_makefile_and_canonical_rtl(tmp_path):
    workflow_id = "fpga-workflow"
    prefix = f"backend/workflows/{workflow_id}"
    paths = {
        f"{prefix}/vv/tb/simulation_manifest.json": json.dumps({"top_module": "product_top"}).encode(),
        f"{prefix}/vv/tb/Makefile": b"all:\n\t@echo pass\n",
        f"{prefix}/vv/tb/rtl_sources.mk": b"VERILOG_SOURCES += ../../handoff/rtl/product_top.sv\n",
        f"{prefix}/fpga/handoff/rtl/product_top.sv": b"module product_top; endmodule\n",
    }
    row = {"id": workflow_id, "user_id": "user", "artifacts": list(paths)}
    state = {"supabase_client": _Supabase(row, paths)}

    bundle = ingest._restore_verified_simulation_bundle(state, str(tmp_path), workflow_id)

    assert bundle["status"] == "ready"
    assert bundle["top_module"] == "product_top"
    assert Path(bundle["makefile_path"]).parts[-3:] == ("vv", "tb", "Makefile")
    assert len(bundle["rtl_files"]) == 1
    assert Path(bundle["rtl_files"][0]).parts[-3:] == ("handoff", "rtl", "product_top.sv")


def test_verified_simulation_bundle_rejects_path_traversal(tmp_path):
    workflow_id = "fpga-workflow"
    path = f"backend/workflows/{workflow_id}/vv/tb/../../outside.py"
    row = {"id": workflow_id, "user_id": "user", "artifacts": [path]}

    bundle = ingest._restore_verified_simulation_bundle(
        {"supabase_client": _Supabase(row, {path: b"bad"})},
        str(tmp_path),
        workflow_id,
    )

    assert bundle["status"] == "incomplete"
    assert not (tmp_path / "outside.py").exists()


def test_verified_simulation_bundle_deduplicates_same_rtl_from_multiple_handoffs(tmp_path):
    workflow_id = "fpga-workflow"
    prefix = f"backend/workflows/{workflow_id}"
    paths = {
        f"{prefix}/vv/tb/simulation_manifest.json": json.dumps({"top_module": "product_top"}).encode(),
        f"{prefix}/vv/tb/Makefile": b"all:\n\t@echo pass\n",
        f"{prefix}/fpga/handoff/rtl/product_top.sv": b"module product_top; endmodule\n",
        f"{prefix}/verification/handoff/rtl/product_top.sv": b"module product_top; endmodule\n",
    }
    row = {"id": workflow_id, "user_id": "user", "artifacts": list(paths)}

    bundle = ingest._restore_verified_simulation_bundle(
        {"supabase_client": _Supabase(row, paths)}, str(tmp_path), workflow_id
    )

    assert bundle["status"] == "ready"
    assert len(bundle["rtl_files"]) == 1


def test_verified_simulation_bundle_restores_imported_rtl_used_by_makefile(tmp_path):
    workflow_id = "mixed-signal-workflow"
    prefix = f"backend/workflows/{workflow_id}"
    paths = {
        f"{prefix}/vv/tb/simulation_manifest.json": json.dumps({"top_module": "temp_monitor_soc_sim"}).encode(),
        f"{prefix}/vv/tb/Makefile": b"include rtl_sources.mk\n",
        f"{prefix}/vv/tb/rtl_sources.mk": b"VERILOG_SOURCES += ../../system/imported_rtl/temp_sensor_adc_model.v\n",
        f"{prefix}/system/imported_rtl/temp_sensor_adc_model.v": b"module temp_sensor_adc_model; endmodule\n",
    }
    row = {"id": workflow_id, "user_id": "user", "artifacts": list(paths)}

    bundle = ingest._restore_verified_simulation_bundle(
        {"supabase_client": _Supabase(row, paths)}, str(tmp_path), workflow_id
    )

    restored_model = Path(bundle["restore_root"]) / "system" / "imported_rtl" / "temp_sensor_adc_model.v"
    assert bundle["status"] == "ready"
    assert restored_model.is_file()
    assert str(restored_model) in bundle["rtl_files"]
