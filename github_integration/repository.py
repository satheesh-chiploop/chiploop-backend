from typing import Any, Dict, List, Optional


class GitHubInstallationRepository:
    def get_active_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def upsert_installation(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def disconnect(self, user_id: str, installation_id: str) -> bool:
        raise NotImplementedError


class InMemoryGitHubInstallationRepository(GitHubInstallationRepository):
    def __init__(self):
        self.rows: Dict[str, Dict[str, Any]] = {}

    def get_active_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        rows = [row for row in self.rows.values() if row.get("user_id") == user_id and row.get("status") == "active"]
        return rows[0] if rows else None

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return [row for row in self.rows.values() if row.get("user_id") == user_id and row.get("status") == "active"]

    def upsert_installation(self, row: Dict[str, Any]) -> Dict[str, Any]:
        key = f"{row.get('user_id')}:{row.get('github_installation_id')}"
        existing = self.rows.get(key) or {}
        saved = {**existing, **row, "id": existing.get("id") or key}
        self.rows[key] = saved
        return saved

    def disconnect(self, user_id: str, installation_id: str) -> bool:
        key = f"{user_id}:{installation_id}"
        row = self.rows.get(key)
        if not row:
            return False
        row["status"] = "disconnected"
        return True


class SupabaseGitHubInstallationRepository(GitHubInstallationRepository):
    def __init__(self, supabase):
        self.supabase = supabase

    def get_active_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        res = (
            self.supabase.table("github_installations")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        return (res.data or [None])[0]

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        res = (
            self.supabase.table("github_installations")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .execute()
        )
        return res.data or []

    def upsert_installation(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = (
            self.supabase.table("github_installations")
            .upsert(row, on_conflict="user_id,github_installation_id")
            .execute()
        )
        data = res.data or []
        return data[0] if data else row

    def disconnect(self, user_id: str, installation_id: str) -> bool:
        existing = self.get_active_for_user(user_id)
        if not existing or str(existing.get("github_installation_id")) != str(installation_id):
            return False
        self.supabase.table("github_installations").update({"status": "disconnected"}).eq("user_id", user_id).eq("github_installation_id", installation_id).execute()
        return True
