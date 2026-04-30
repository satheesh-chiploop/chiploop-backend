from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from user_agents.service import UserAgentService

from .repository import MarketplaceRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "agent"


def _agent_from_submission(submission: Dict[str, Any]) -> Dict[str, Any]:
    agent = submission.get("agent_json")
    return agent if isinstance(agent, dict) else {}


class MarketplaceService:
    def __init__(self, repository: MarketplaceRepository, user_agents: UserAgentService):
        self.repository = repository
        self.user_agents = user_agents

    def list_agents(self, query: str = "", loop_type: str = "", domain: str = "") -> List[Dict[str, Any]]:
        return self.repository.list_listings(query=query, loop_type=loop_type, domain=domain)

    def get_agent(self, listing_id_or_slug: str) -> Dict[str, Any] | None:
        listing = self.repository.get_listing(listing_id_or_slug)
        if not listing:
            return None
        reviews = self.repository.list_reviews(str(listing["id"]))
        return {**listing, "reviews": reviews}

    def install_agent(self, user_id: str, listing_id_or_slug: str) -> Dict[str, Any]:
        listing = self.repository.get_listing(listing_id_or_slug)
        if not listing or listing.get("status") != "active":
            return {"ok": False, "reason": "listing_not_found"}

        installed = self.user_agents.save_private_agent(
            user_id,
            {
                "name": listing.get("name"),
                "loop_type": listing.get("loop_type") or "system",
                "description": listing.get("description") or "",
                "agent_spec": listing.get("agent_spec") or {},
                "skills": listing.get("skills") or [],
                "tools": listing.get("tools") or [],
                "hooks": listing.get("hooks") or [],
                "source": "marketplace_install",
                "status": "private",
                "visibility": "private",
            },
        )
        install = self.repository.create_install(
            {
                "user_id": user_id,
                "listing_id": listing["id"],
                "version": listing.get("version") or "1.0.0",
                "installed_agent_id": installed.get("id"),
                "status": "installed",
                "created_at": _now(),
            }
        )
        return {"ok": True, "listing": listing, "installed_agent": installed, "install": install}

    def list_reviews(self, listing_id_or_slug: str) -> List[Dict[str, Any]]:
        listing = self.repository.get_listing(listing_id_or_slug)
        if not listing:
            return []
        return self.repository.list_reviews(str(listing["id"]))

    def review_agent(self, user_id: str, listing_id_or_slug: str, rating: int, text: str = "") -> Dict[str, Any]:
        listing = self.repository.get_listing(listing_id_or_slug)
        if not listing:
            return {"ok": False, "reason": "listing_not_found"}
        rating = max(1, min(5, int(rating)))
        review = self.repository.create_review(
            {
                "listing_id": listing["id"],
                "user_id": user_id,
                "rating": rating,
                "review_text": text,
                "hidden": False,
                "updated_at": _now(),
            }
        )
        return {"ok": True, "review": review}

    def list_submissions(self, status: str = "") -> List[Dict[str, Any]]:
        return self.repository.list_submissions(status=status)

    def get_submission(self, submission_id: str) -> Dict[str, Any] | None:
        return self.repository.get_submission(submission_id)

    def approve_submission(self, submission_id: str, reviewer_id: str, notes: str = "") -> Dict[str, Any]:
        submission = self.repository.get_submission(submission_id)
        if not submission:
            return {"ok": False, "reason": "submission_not_found"}

        agent = _agent_from_submission(submission)
        name = str(agent.get("agent_name") or agent.get("name") or "Marketplace Agent")
        agent_spec = agent.get("agent_spec") if isinstance(agent.get("agent_spec"), dict) else agent
        listing = self.repository.create_listing(
            {
                "source_agent_id": submission.get("agent_id") or agent.get("id"),
                "approved_submission_id": submission.get("id") or submission_id,
                "name": name,
                "slug": _slug(name),
                "description": agent.get("description") or agent_spec.get("description") or "",
                "domain": agent.get("domain") or agent.get("loop_type") or agent_spec.get("domain") or "system",
                "loop_type": agent.get("loop_type") or agent_spec.get("loop_type") or "system",
                "agent_spec": agent_spec,
                "skills": agent.get("skills") or agent_spec.get("required_skills") or [],
                "tools": agent.get("tools") or agent_spec.get("required_tools") or [],
                "hooks": agent.get("hooks") or agent_spec.get("hooks") or [],
                "version": "1.0.0",
                "publisher_user_id": agent.get("owner_id") or submission.get("submitted_by"),
                "visibility": "public",
                "status": "active",
                "install_count": 0,
                "average_rating": 0,
                "review_count": 0,
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
        version = self.repository.create_version(
            {
                "listing_id": listing["id"],
                "version": "1.0.0",
                "agent_spec": listing.get("agent_spec") or {},
                "skills": listing.get("skills") or [],
                "tools": listing.get("tools") or [],
                "hooks": listing.get("hooks") or [],
                "generated_files": agent.get("generated_files") or [],
                "registry_patch": agent.get("registry_patch") or {},
                "release_notes": notes,
                "created_at": _now(),
            }
        )
        patch = {
            "status": "approved",
            "reviewed_at": _now(),
            "reviewed_by": reviewer_id,
            "review_notes": notes,
        }
        self.repository.update_submission(submission_id, patch)
        if submission.get("agent_id"):
            self.repository.update_agent(str(submission["agent_id"]), {"status": "approved", "reviewed_at": _now(), "reviewed_by": reviewer_id, "review_notes": notes})
        return {"ok": True, "listing": listing, "version": version}

    def reject_submission(self, submission_id: str, reviewer_id: str, notes: str = "", *, changes_requested: bool = False) -> Dict[str, Any]:
        status = "changes_requested" if changes_requested else "rejected"
        patch = {
            "status": status,
            "reviewed_at": _now(),
            "reviewed_by": reviewer_id,
            "review_notes": notes,
        }
        updated = self.repository.update_submission(submission_id, patch)
        if not updated:
            return {"ok": False, "reason": "submission_not_found"}
        if updated.get("agent_id"):
            self.repository.update_agent(str(updated["agent_id"]), {"status": status, "reviewed_at": _now(), "reviewed_by": reviewer_id, "review_notes": notes})
        return {"ok": True, "submission": updated}
