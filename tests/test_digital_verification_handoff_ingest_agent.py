import json
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_verification_handoff_ingest_agent as agent


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

    def execute(self):
        return _Response(self._data)


class _Storage:
    def __init__(self, files):
        self._files = files

    def download(self, path):
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


class _StorageClient:
    def __init__(self, files):
        self._files = files

    def from_(self, _bucket):
        return _Storage(self._files)


class _Client:
    def __init__(self, artifacts, files):
        self._artifacts = artifacts
        self.storage = _StorageClient(files)

    def table(self, _name):
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


def test_rejects_verify_run_without_arch2rtl_source():
    try:
        agent.run_agent({"workflow_id": "verify-wf", "rtl_source_mode": "paste"})
    except RuntimeError as exc:
        assert "completed Arch2RTL workflow source" in str(exc)
    else:
        raise AssertionError("Expected Verify source validation failure")
