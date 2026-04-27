import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chiploop_sdk.cli import main as cli_main
from chiploop_sdk.client import ChipLoopClient, _safe_artifact_target
from chiploop_sdk.errors import ChipLoopAPIError, ChipLoopConfigError


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", content=b"", content_type="application/json"):
        self.status_code = status_code
        self._data = data
        self.text = text or json.dumps(data or {})
        self.content = content or self.text.encode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def test_sdk_client_initialization_from_env(monkeypatch):
    monkeypatch.setenv("CHIPLOOP_BASE_URL", "https://chiploop.example")
    monkeypatch.setenv("CHIPLOOP_API_KEY", "token")

    client = ChipLoopClient(session=FakeSession(FakeResponse()))

    assert client.base_url == "https://chiploop.example"
    assert client.api_key == "token"


def test_sdk_missing_base_url_error(monkeypatch):
    monkeypatch.delenv("CHIPLOOP_BASE_URL", raising=False)

    with pytest.raises(ChipLoopConfigError):
        ChipLoopClient()


def test_sdk_api_error_handling():
    client = ChipLoopClient(
        "https://chiploop.example",
        session=FakeSession(FakeResponse(status_code=500, data={"detail": "failed"}, text="failed")),
    )

    with pytest.raises(ChipLoopAPIError):
        client.list_workflows()


def test_run_workflow_request_construction():
    session = FakeSession(FakeResponse(data={"workflow_id": "wf1", "run_id": "run1", "status": "queued"}))
    client = ChipLoopClient("https://chiploop.example", api_key="token", session=session)

    result = client.run_workflow("digital", spec_text="module top; endmodule", inputs={"loop_type": "digital"})

    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://chiploop.example/run_workflow"
    assert json.loads(kwargs["data"]["workflow"])["loop_type"] == "digital"
    assert kwargs["data"]["spec_text"] == "module top; endmodule"
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert result.workflow_id == "wf1"


def test_artifact_output_path_safety(tmp_path):
    target = _safe_artifact_target(str(tmp_path), "reports/out.txt")
    assert target == tmp_path.resolve() / "reports" / "out.txt"

    with pytest.raises(ChipLoopConfigError):
        _safe_artifact_target(str(tmp_path), "../secret.txt")


def test_cli_workflows_list(monkeypatch, capsys):
    monkeypatch.setenv("CHIPLOOP_BASE_URL", "https://chiploop.example")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_workflows(self):
            return {"workflows": [{"name": "digital"}], "count": 1}

    monkeypatch.setattr("chiploop_sdk.cli.ChipLoopClient", FakeClient)

    assert cli_main(["workflows", "list", "--json"]) == 0
    assert "\"count\": 1" in capsys.readouterr().out


def test_cli_run_uses_spec_text(monkeypatch, capsys):
    calls = {}

    class FakeRun:
        def to_dict(self):
            return {"workflow_id": "wf1", "status": "queued"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def run_workflow(self, workflow, **kwargs):
            calls["workflow"] = workflow
            calls["kwargs"] = kwargs
            return FakeRun()

    monkeypatch.setattr("chiploop_sdk.cli.ChipLoopClient", FakeClient)

    assert cli_main(["--base-url", "http://local", "run", "digital", "--spec-text", "spec", "--json"]) == 0
    assert calls["workflow"] == "digital"
    assert calls["kwargs"]["spec_text"] == "spec"
    assert "workflow_id" in capsys.readouterr().out


def test_cli_studio_plan_agent_request_loading(tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"natural_language_request": "Digital RTL Agent"}), encoding="utf-8")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def plan_agent(self, request):
            return {"requested_capability": request["natural_language_request"]}

    monkeypatch.setattr("chiploop_sdk.cli.ChipLoopClient", FakeClient)

    assert cli_main(["--base-url", "http://local", "studio", "plan-agent", "--request", str(request), "--json"]) == 0
    assert "Digital RTL Agent" in capsys.readouterr().out


def test_cli_studio_generate_agent_defaults_to_dry_run(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"name": "New Agent", "natural_language_request": "new"}), encoding="utf-8")
    calls = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def generate_agent(self, request, *, dry_run=True):
            calls["dry_run"] = dry_run
            return {"ok": True, "dry_run": dry_run}

    monkeypatch.setattr("chiploop_sdk.cli.ChipLoopClient", FakeClient)

    assert cli_main(["--base-url", "http://local", "studio", "generate-agent", "--request", str(request)]) == 0
    assert calls["dry_run"] is True
