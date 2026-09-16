import json
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_cosim_ingest_agent as ingest
from agents.system import system_software_handoff_package_agent as handoff


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


def test_simulation_bundle_falls_back_from_fpga_handoff_to_verified_rtl_flow(tmp_path, monkeypatch):
    calls = []

    def fake_restore(_state, _workflow_dir, workflow_id):
        calls.append(workflow_id)
        if workflow_id == "fpga-integration":
            return {
                "status": "incomplete",
                "source_workflow_id": workflow_id,
                "restored_file_count": 7,
                "rtl_files": ["integrated_top.sv"],
            }
        return {
            "status": "ready",
            "source_workflow_id": workflow_id,
            "restored_file_count": 12,
            "makefile_path": "vv/tb/Makefile",
            "rtl_files": ["core.v"],
        }

    monkeypatch.setattr(ingest, "_restore_verified_simulation_bundle", fake_restore)

    bundle = ingest._restore_first_verified_simulation_bundle(
        {}, str(tmp_path), ["fpga-integration", "fpga-integration", "rtl-generation"]
    )

    assert calls == ["fpga-integration", "rtl-generation"]
    assert bundle["status"] == "ready"
    assert bundle["source_workflow_id"] == "rtl-generation"
    assert bundle["resolution_attempts"] == [
        {
            "source_workflow_id": "fpga-integration",
            "status": "incomplete",
            "restored_file_count": 7,
            "reason": "",
        },
        {
            "source_workflow_id": "rtl-generation",
            "status": "ready",
            "restored_file_count": 12,
            "reason": "",
        },
    ]


def test_software_handoff_discovers_explicit_integration_intent_and_deduplicates_rtl_modules(tmp_path):
    intent = tmp_path / "integration.json"
    intent.write_text('{"intent_type":"system_integration"}', encoding="utf-8")
    first = tmp_path / "rtl" / "core.v"
    duplicate = tmp_path / "system" / "imported_rtl" / "core.v"
    wrapper = tmp_path / "rtl" / "wrapper.sv"
    first.parent.mkdir(parents=True)
    duplicate.parent.mkdir(parents=True)
    first.write_text("module core; endmodule\n", encoding="utf-8")
    duplicate.write_text("module core; endmodule\n", encoding="utf-8")
    wrapper.write_text("module wrapper; core u_core(); endmodule\n", encoding="utf-8")

    state = {
        "system_integration_intent_path": str(intent),
        "rtl_inputs": [str(first), str(duplicate), str(wrapper)],
    }
    resolved_intent = handoff._find_system_integration_intent_path(state, str(tmp_path), None, [])
    _, rtl_files = handoff._find_rtl_filelist(state, str(tmp_path))

    assert resolved_intent == str(intent).replace("\\", "/")
    assert rtl_files == [str(first).replace("\\", "/"), str(wrapper).replace("\\", "/")]
