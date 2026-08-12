import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_product_collateral_ingest_agent as ingest


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, data):
        self._data = data

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def single(self):
        return self

    def execute(self):
        return _Result(self._data)


class _StorageBucket:
    def __init__(self, payloads):
        self._payloads = payloads

    def download(self, path):
        payload = self._payloads.get(path)
        return json.dumps(payload).encode("utf-8") if payload else None


class _Storage:
    def __init__(self, payloads):
        self._payloads = payloads

    def from_(self, _bucket):
        return _StorageBucket(self._payloads)


class _Supabase:
    def __init__(self, row, payloads):
        self._row = row
        self.storage = _Storage(payloads)

    def table(self, _name):
        return _Query(self._row)


def test_product_ingest_loads_l2_validation_summary_from_nested_artifact_path():
    workflow_id = "validation-workflow"
    path = (
        "backend/workflows/validation-workflow/"
        "system/software_validation/cosim/summary/system_software_validation_summary_l2.json"
    )
    row = {"id": workflow_id, "artifacts": {"summary": path}}
    payload = {"package_type": "system_software_validation_summary_l2", "overall_status": "pass"}

    result = ingest._workflow_artifact_json(
        {"supabase_client": _Supabase(row, {path: payload})},
        workflow_id,
        ingest.VALIDATION_ARTIFACT_CANDIDATES,
    )

    assert result["path"] == path
    assert result["data"] == payload


def test_product_signoff_requires_completed_lineage_and_passing_cosim():
    lineage = {
        "arch2rtl_workflow_id": "rtl",
        "firmware_workflow_id": "fw",
        "software_workflow_id": "sw",
        "validation_workflow_id": "val",
    }
    checks = {key: {"status": "completed"} for key in lineage}
    artifacts = {
        "firmware_register_map": {"data": {"registers": []}},
        "software_handoff": {"data": {"type": "handoff"}},
        "software_api": {"data": {"type": "api"}},
        "software_package": {"data": {"type": "package"}},
        "validation_summary": {"data": {
            "final_system_correctness_verdict": "pass",
            "scenario_fail_count": 0,
            "scenario_blocked_count": 0,
        }},
    }

    assert ingest._product_signoff(lineage, checks, artifacts)["status"] == "pass"


def test_product_signoff_rejects_completed_workflow_with_blocked_validation():
    lineage = {
        "arch2rtl_workflow_id": "rtl",
        "firmware_workflow_id": "fw",
        "software_workflow_id": "sw",
        "validation_workflow_id": "val",
    }
    checks = {key: {"status": "completed"} for key in lineage}
    artifacts = {
        "firmware_register_map": {"data": {}},
        "software_handoff": {"data": {}},
        "software_api": {"data": {}},
        "software_package": {"data": {}},
        "validation_summary": {"data": {
            "final_system_correctness_verdict": "blocked",
            "scenario_blocked_count": 1,
        }},
    }

    signoff = ingest._product_signoff(lineage, checks, artifacts)
    assert signoff["status"] == "fail"
    assert "validation verdict is blocked, expected pass" in signoff["issues"]
    assert "validation contains blocked scenarios" in signoff["issues"]


def test_failed_product_signoff_publishes_contract_before_raising(monkeypatch):
    published = {}
    monkeypatch.setattr(ingest, "_record", lambda _workflow_id, filename, obj: published.update({filename: obj}))
    monkeypatch.setattr(ingest, "_workflow_status", lambda _state, workflow_id: {"id": workflow_id, "status": "failed"})
    monkeypatch.setattr(ingest, "_workflow_artifact_json", lambda *_args, **_kwargs: {})

    state = {
        "workflow_id": "product",
        "system_rtl_workflow_id": "rtl",
        "system_firmware_workflow_id": "fw",
        "system_software_workflow_id": "sw",
        "system_validation_workflow_id": "val",
        "_require_product_upstream_signoff": True,
    }

    import pytest
    with pytest.raises(RuntimeError, match="Product upstream signoff failed"):
        ingest.run_agent(state)

    assert published["system_product_collateral_contract.json"]["upstream_signoff"]["status"] == "fail"
