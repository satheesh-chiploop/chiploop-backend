import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_routes
from webinar import InMemoryWebinarRegistrationRepository, WebinarRegistrationService


def _client(repo: InMemoryWebinarRegistrationRepository | None = None) -> tuple[TestClient, InMemoryWebinarRegistrationRepository]:
    repository = repo or InMemoryWebinarRegistrationRepository()
    app = FastAPI()
    app.state.webinar_service = WebinarRegistrationService(repository)
    app.include_router(browser_routes.router)
    return TestClient(app), repository


def test_webinar_registration_saves_basic_details():
    client, repo = _client()
    response = client.post(
        "/webinar/register",
        json={
            "name": "Ada Lovelace",
            "email": "Ada@Example.com",
            "company": "ChipLoop",
            "role": "Student",
            "loop_interest": "Digital",
            "preferred_session": "9am_pst",
            "questions": "Will you show RTL outputs?",
        },
    )

    assert response.status_code == 200
    registration_id = response.json()["registration"]["id"]
    saved = repo.records[registration_id]
    assert saved.email == "ada@example.com"
    assert saved.preferred_session == "9am_pst"
    assert saved.loop_interest == "Digital"


def test_webinar_registration_validates_required_fields():
    client, _ = _client()
    response = client.post(
        "/webinar/register",
        json={"name": "", "email": "bad", "preferred_session": "nope"},
    )

    assert response.status_code == 400
