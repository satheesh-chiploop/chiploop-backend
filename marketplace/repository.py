from __future__ import annotations

from typing import Any, Dict, List, Optional


class MarketplaceRepository:
    def list_listings(self, query: str = "", loop_type: str = "", domain: str = "") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_listing(self, listing_id_or_slug: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def create_listing(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def create_version(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def create_install(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def create_review(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_reviews(self, listing_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def list_submissions(self, status: str = "") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def update_submission(self, submission_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def update_agent(self, agent_id: str, patch: Dict[str, Any]) -> None:
        raise NotImplementedError


class SupabaseMarketplaceRepository(MarketplaceRepository):
    def __init__(self, supabase):
        self.supabase = supabase

    def list_listings(self, query: str = "", loop_type: str = "", domain: str = "") -> List[Dict[str, Any]]:
        q = self.supabase.table("marketplace_agent_listings").select("*").eq("status", "active")
        if loop_type:
            q = q.eq("loop_type", loop_type)
        if domain:
            q = q.eq("domain", domain)
        res = q.order("updated_at", desc=True).execute()
        rows = res.data or []
        if query:
            needle = query.lower()
            rows = [
                row for row in rows
                if needle in str(row.get("name") or "").lower()
                or needle in str(row.get("description") or "").lower()
                or needle in str(row.get("skills") or "").lower()
                or needle in str(row.get("tools") or "").lower()
            ]
        return rows

    def get_listing(self, listing_id_or_slug: str) -> Optional[Dict[str, Any]]:
        table = self.supabase.table("marketplace_agent_listings").select("*")
        res = table.eq("id", listing_id_or_slug).limit(1).execute()
        data = res.data or []
        if data:
            return data[0]
        res = self.supabase.table("marketplace_agent_listings").select("*").eq("slug", listing_id_or_slug).limit(1).execute()
        data = res.data or []
        return data[0] if data else None

    def create_listing(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.supabase.table("marketplace_agent_listings").insert(row).execute()
        data = res.data or []
        return data[0] if data else row

    def create_version(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.supabase.table("marketplace_agent_versions").insert(row).execute()
        data = res.data or []
        return data[0] if data else row

    def create_install(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.supabase.table("marketplace_installs").insert(row).execute()
        data = res.data or []
        return data[0] if data else row

    def create_review(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.supabase.table("marketplace_reviews").upsert(row, on_conflict="listing_id,user_id").execute()
        data = res.data or []
        return data[0] if data else row

    def list_reviews(self, listing_id: str) -> List[Dict[str, Any]]:
        res = self.supabase.table("marketplace_reviews").select("*").eq("listing_id", listing_id).eq("hidden", False).order("created_at", desc=True).execute()
        return res.data or []

    def list_submissions(self, status: str = "") -> List[Dict[str, Any]]:
        q = self.supabase.table("marketplace_submissions").select("*")
        if status:
            q = q.eq("status", status)
        res = q.order("created_at", desc=True).execute()
        return res.data or []

    def get_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        res = self.supabase.table("marketplace_submissions").select("*").eq("id", submission_id).limit(1).execute()
        data = res.data or []
        if data:
            return data[0]
        res = self.supabase.table("marketplace_submissions").select("*").eq("submission_id", submission_id).limit(1).execute()
        data = res.data or []
        return data[0] if data else None

    def update_submission(self, submission_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        res = self.supabase.table("marketplace_submissions").update(patch).eq("id", submission_id).execute()
        data = res.data or []
        return data[0] if data else None

    def update_agent(self, agent_id: str, patch: Dict[str, Any]) -> None:
        self.supabase.table("agents").update(patch).eq("id", agent_id).execute()


class InMemoryMarketplaceRepository(MarketplaceRepository):
    def __init__(self):
        self.listings: Dict[str, Dict[str, Any]] = {}
        self.versions: List[Dict[str, Any]] = []
        self.installs: List[Dict[str, Any]] = []
        self.reviews: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.submissions: Dict[str, Dict[str, Any]] = {}
        self.next_id = 1

    def _id(self, prefix: str) -> str:
        value = f"{prefix}-{self.next_id}"
        self.next_id += 1
        return value

    def list_listings(self, query: str = "", loop_type: str = "", domain: str = "") -> List[Dict[str, Any]]:
        rows = [row for row in self.listings.values() if row.get("status") == "active"]
        if loop_type:
            rows = [row for row in rows if row.get("loop_type") == loop_type]
        if domain:
            rows = [row for row in rows if row.get("domain") == domain]
        if query:
            needle = query.lower()
            rows = [row for row in rows if needle in str(row).lower()]
        return rows

    def get_listing(self, listing_id_or_slug: str) -> Optional[Dict[str, Any]]:
        if listing_id_or_slug in self.listings:
            return self.listings[listing_id_or_slug]
        return next((row for row in self.listings.values() if row.get("slug") == listing_id_or_slug), None)

    def create_listing(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = {"id": self._id("listing"), **row}
        self.listings[item["id"]] = item
        return item

    def create_version(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = {"id": self._id("version"), **row}
        self.versions.append(item)
        return item

    def create_install(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = {"id": self._id("install"), **row}
        self.installs.append(item)
        return item

    def create_review(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = {"id": self._id("review"), **row}
        self.reviews[(row["listing_id"], row["user_id"])] = item
        return item

    def list_reviews(self, listing_id: str) -> List[Dict[str, Any]]:
        return [row for (lid, _), row in self.reviews.items() if lid == listing_id and not row.get("hidden")]

    def list_submissions(self, status: str = "") -> List[Dict[str, Any]]:
        rows = list(self.submissions.values())
        return [row for row in rows if not status or row.get("status") == status]

    def get_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        return self.submissions.get(submission_id)

    def update_submission(self, submission_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if submission_id not in self.submissions:
            return None
        self.submissions[submission_id].update(patch)
        return self.submissions[submission_id]

    def update_agent(self, agent_id: str, patch: Dict[str, Any]) -> None:
        return None
