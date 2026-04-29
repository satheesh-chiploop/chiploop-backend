from typing import Any, Dict, List, Optional


AGENT_SELECT_COLUMNS = (
    "id,owner_id,agent_name,loop_type,description,script_path,is_custom,is_prebuilt,"
    "is_marketplace,agent_spec,skills,tools,hooks,generated_files,registry_patch,"
    "status,visibility,source,submitted_at,reviewed_at,reviewed_by,review_notes,"
    "created_at,updated_at"
)


class UserAgentRepository:
    def list_by_owner(self, user_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def list_global_visible(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def insert_private(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def get_owned(self, user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def delete_owned(self, user_id: str, agent_id: str) -> bool:
        raise NotImplementedError

    def update_owned(self, user_id: str, agent_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def create_marketplace_submission(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class SupabaseUserAgentRepository(UserAgentRepository):
    def __init__(self, supabase):
        self.supabase = supabase

    def list_by_owner(self, user_id: str) -> List[Dict[str, Any]]:
        res = (
            self.supabase.table("agents")
            .select(AGENT_SELECT_COLUMNS)
            .eq("owner_id", user_id)
            .eq("is_custom", True)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []

    def list_global_visible(self) -> List[Dict[str, Any]]:
        query = self.supabase.table("agents").select(AGENT_SELECT_COLUMNS)
        try:
            res = query.or_("visibility.in.(global,marketplace),status.eq.approved,is_marketplace.eq.true,is_prebuilt.eq.true").execute()
        except Exception:
            res = (
                self.supabase.table("agents")
                .select(AGENT_SELECT_COLUMNS)
                .eq("is_prebuilt", True)
                .execute()
            )
        return res.data or []

    def insert_private(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.supabase.table("agents").insert(row).execute()
        data = res.data or []
        return data[0] if data else row

    def get_owned(self, user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        res = (
            self.supabase.table("agents")
            .select(AGENT_SELECT_COLUMNS)
            .eq("id", agent_id)
            .eq("owner_id", user_id)
            .eq("is_custom", True)
            .limit(1)
            .execute()
        )
        return (res.data or [None])[0]

    def delete_owned(self, user_id: str, agent_id: str) -> bool:
        if not self.get_owned(user_id, agent_id):
            return False
        self.supabase.table("agents").delete().eq("id", agent_id).eq("owner_id", user_id).eq("is_custom", True).execute()
        return True

    def update_owned(self, user_id: str, agent_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.get_owned(user_id, agent_id):
            return None
        res = (
            self.supabase.table("agents")
            .update(patch)
            .eq("id", agent_id)
            .eq("owner_id", user_id)
            .eq("is_custom", True)
            .execute()
        )
        data = res.data or []
        return data[0] if data else self.get_owned(user_id, agent_id)

    def create_marketplace_submission(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        res = self.supabase.table("marketplace_submissions").insert(row).execute()
        data = res.data or []
        return data[0] if data else row
