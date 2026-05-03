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
            "preferred_session": "2026-05-23_09am_pt",
            "questions": "Will you show RTL outputs?",
        },
    )

    assert response.status_code == 200
    registration_id = response.json()["registration"]["id"]
    saved = repo.records[registration_id]
    assert saved.email == "ada@example.com"
    assert saved.preferred_session == "2026-05-23_09am_pt"
    assert saved.loop_interest == "Digital"


def test_webinar_registration_validates_required_fields():
    client, _ = _client()
    response = client.post(
        "/webinar/register",
        json={"name": "", "email": "bad", "preferred_session": "nope"},
    )

    assert response.status_code == 400


def test_webinar_sessions_show_capacity():
    client, _ = _client()

    response = client.get("/webinar/sessions")

    assert response.status_code == 200
    first = response.json()["sessions"][0]
    assert first["id"] == "2026-05-23_09am_pt"
    assert first["capacity"] == 10
    assert first["booked"] == 0
    assert first["full"] is False


def test_webinar_session_full_rejected():
    client, repo = _client()
    for index in range(10):
        response = client.post(
            "/webinar/register",
            json={
                "name": f"User {index}",
                "email": f"user{index}@example.com",
                "preferred_session": "2026-05-23_09am_pt",
            },
        )
        assert response.status_code == 200

    sessions = client.get("/webinar/sessions").json()["sessions"]
    first = sessions[0]
    assert first["booked"] == 10
    assert first["remaining"] == 0
    assert first["full"] is True

    response = client.post(
        "/webinar/register",
        json={
            "name": "Late User",
            "email": "late@example.com",
            "preferred_session": "2026-05-23_09am_pt",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "preferred_session_full"
