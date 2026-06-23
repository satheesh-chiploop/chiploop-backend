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
