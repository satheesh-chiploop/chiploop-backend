import json
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_verification_handoff_ingest_agent as agent
from agents.digital.digital_simulation_execution_agent import _merge_functional_coverage_summaries


class _Response:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, data):
        self._data = data

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def single(self):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return _Response(self._data)


class _Storage:
    def __init__(self, files):
        self._files = files

    def download(self, path):
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def list(self, folder):
        prefix = folder.rstrip("/") + "/"
        names = {
            path[len(prefix):].split("/", 1)[0]
            for path in self._files
            if path.startswith(prefix)
        }
        return [{"name": name} for name in sorted(names)]


class _StorageClient:
    def __init__(self, files):
        self._files = files

    def from_(self, _bucket):
        return _Storage(self._files)


class _Client:
    def __init__(self, artifacts, files, run_rows=None):
        self._artifacts = artifacts
        self._run_rows = run_rows or []
        self.storage = _StorageClient(files)

    def table(self, name):
        if name == "runs":
            return _Table(self._run_rows)
        return _Table({"id": "source-wf", "artifacts": self._artifacts})


def test_imports_arch2rtl_spec_and_rtl_for_verify(tmp_path, monkeypatch):
    spec_path = "backend/workflows/source-wf/spec/pwm_controller_spec.json"
    rtl_path = "backend/workflows/source-wf/rtl/pwm_controller.sv"
    files = {
        spec_path: json.dumps({"name": "pwm_controller", "rtl_output_file": "pwm_controller.sv", "ports": []}).encode(),
        rtl_path: b"module pwm_controller; endmodule\n",
    }
    uploads = []
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: uploads.append((args, kwargs)))

    state = {
        "workflow_id": "verify-wf",
        "workflow_dir": str(tmp_path),
        "rtl_source_mode": "from_arch2rtl",
        "from_workflow_id": "source-wf",
        "supabase_client": _Client({"spec": spec_path, "rtl": rtl_path}, files),
    }

    result = agent.run_agent(state)

    assert result["top_module"] == "pwm_controller"
    assert Path(result["spec_json"]).exists()
    assert [Path(path).name for path in result["rtl_files"]] == ["pwm_controller.sv"]
    assert result["verification_source_handoff"]["source_workflow_id"] == "source-wf"
    assert len(uploads) == 3


def test_imports_from_storage_when_workflow_index_has_lost_spec_path(tmp_path, monkeypatch):
    spec_path = "backend/workflows/source-wf/spec/pwm_controller_spec.json"
    rtl_path = "backend/workflows/source-wf/rtl/pwm_controller.sv"
    files = {
        spec_path: json.dumps({"name": "pwm_controller", "rtl_output_file": "pwm_controller.sv", "ports": []}).encode(),
        rtl_path: b"module pwm_controller; endmodule\n",
    }
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    state = {
        "workflow_id": "verify-wf",
        "workflow_dir": str(tmp_path),
        "rtl_source_mode": "from_arch2rtl",
        "from_workflow_id": "source-wf",
        "supabase_client": _Client({"__mode": "prefix", "__prefix": "backend/workflows/source-wf/"}, files),
    }

    result = agent.run_agent(state)

    assert result["verification_source_handoff"]["spec_source_path"] == spec_path
    assert result["verification_source_handoff"]["rtl_source_paths"] == [rtl_path]


def test_imports_rtl_from_packaged_storage_subdirectory(tmp_path, monkeypatch):
    spec_path = "backend/workflows/source-wf/spec/pwm_controller_spec.json"
    rtl_path = "backend/workflows/source-wf/handoff/pwm_controller_ip_package/rtl/pwm_controller.v"
    files = {
        spec_path: json.dumps({"name": "pwm_controller", "rtl_output_file": "pwm_controller.v", "ports": []}).encode(),
        rtl_path: b"module pwm_controller; endmodule\n",
    }
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    state = {
        "workflow_id": "verify-wf",
        "workflow_dir": str(tmp_path),
        "rtl_source_mode": "from_arch2rtl",
        "from_workflow_id": "source-wf",
        "supabase_client": _Client({"__mode": "prefix", "__prefix": "backend/workflows/source-wf/"}, files),
    }

    result = agent.run_agent(state)

    assert result["verification_source_handoff"]["rtl_source_paths"] == [rtl_path]


def test_imports_rtl_from_arch2rtl_app_working_directory(tmp_path, monkeypatch):
    spec_path = "backend/workflows/source-wf/spec/pwm_controller_spec.json"
    artifact_root = tmp_path / "source-run" / "digital"
    rtl_path = artifact_root / "arch2rtl" / "rtl" / "pwm_controller.v"
    rtl_path.parent.mkdir(parents=True)
    rtl_path.write_text("module pwm_controller; endmodule\n", encoding="utf-8")
    files = {
        spec_path: json.dumps({"name": "pwm_controller", "rtl_output_file": "pwm_controller.v", "ports": []}).encode(),
    }
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    state = {
        "workflow_id": "verify-wf",
        "workflow_dir": str(tmp_path / "verify"),
        "rtl_source_mode": "from_arch2rtl",
        "from_workflow_id": "source-wf",
        "supabase_client": _Client(
            {"__mode": "prefix", "__prefix": "backend/workflows/source-wf/"},
            files,
            [{"artifacts_path": str(artifact_root)}],
        ),
    }

    result = agent.run_agent(state)

    assert Path(result["rtl_files"][0]).read_text(encoding="utf-8") == "module pwm_controller; endmodule\n"
    assert result["verification_source_handoff"]["rtl_source_paths"] == [str(rtl_path.resolve())]


def test_rejects_verify_run_without_arch2rtl_source():
    try:
        agent.run_agent({"workflow_id": "verify-wf", "rtl_source_mode": "paste"})
    except RuntimeError as exc:
        assert "completed Arch2RTL workflow source" in str(exc)
    else:
        raise AssertionError("Expected Verify source validation failure")


def test_functional_coverage_is_aggregated_across_regression_runs():
    merged = _merge_functional_coverage_summaries([
        {
            "top_module": "pwm_controller",
            "outputs": {"pwm_out": {"samples": 5, "seen_values": [0], "total_bins": 2}},
            "inputs": {},
        },
        {
            "top_module": "pwm_controller",
            "outputs": {"pwm_out": {"samples": 7, "seen_values": [1], "total_bins": 2}},
            "inputs": {},
        },
    ])

    assert merged["outputs"]["pwm_out"]["seen_values"] == [0, 1]
    assert merged["outputs"]["pwm_out"]["samples"] == 12
    assert merged["functional_coverage_pct"] == 100.0
    assert merged["aggregated_run_count"] == 2
