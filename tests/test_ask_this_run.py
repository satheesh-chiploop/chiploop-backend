import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_routes
from browser_auth import BrowserUser, require_browser_user


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def single(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self.row)


class _FakeBucket:
    def __init__(self, files):
        self.files = files

    def download(self, path):
        return self.files[path].encode("utf-8")


class _FakeStorage:
    def __init__(self, files):
        self.files = files

    def from_(self, _bucket):
        return _FakeBucket(self.files)


class _FakeSupabase:
    def __init__(self):
        self.workflow = {
            "id": "wf-1",
            "user_id": "user-1",
            "name": "Arch2RTL",
            "status": "completed",
            "loop_type": "digital",
            "created_at": "2026-05-08T00:00:00Z",
            "logs": "RTL compile passed\nGenerated handoff artifacts",
            "artifacts": {
                "Digital RTL Agent": {
                    "summary": "backend/workflows/wf-1/rtl/rtl_agent_summary.txt",
                }
            },
        }
        self.storage = _FakeStorage({
            "backend/workflows/wf-1/rtl/rtl_agent_summary.txt": "No lint errors. RTL handoff ready.",
        })

    def table(self, name):
        assert name == "workflows"
        return _FakeQuery(self.workflow)


def _client(monkeypatch):
    app = FastAPI()
    app.state.supabase = _FakeSupabase()
    app.dependency_overrides[require_browser_user] = lambda: BrowserUser(user_id="user-1", claims={"sub": "user-1"})
    app.include_router(browser_routes.router)

    async def fake_llm(prompt: str) -> str:
        assert "RTL compile passed" in prompt
        assert "No lint errors" in prompt
        return "The run completed successfully. RTL handoff looks ready based on rtl_agent_summary.txt."

    monkeypatch.setattr(browser_routes, "_run_inspection_llm", fake_llm)
    return TestClient(app)


def test_ask_this_run_answers_from_logs_and_artifacts(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/workflow/wf-1/ask", json={"question": "Is this ready for handoff?"})

    assert response.status_code == 200
    data = response.json()
    assert data["workflow_id"] == "wf-1"
    assert "handoff" in data["answer"].lower()
    assert data["source_count"] >= 2
    assert any(source["path"].endswith("rtl_agent_summary.txt") for source in data["sources"])


def test_ask_this_run_requires_question(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/workflow/wf-1/ask", json={"question": ""})

    assert response.status_code == 400
    assert response.json()["detail"] == "question_required"
