# services/validation_plan_resolver.py
from __future__ import annotations

from typing import Any, Dict, Optional


def load_active_plan_by_name(supabase, user_id: str, test_plan_name: str) -> Optional[Dict[str, Any]]:
    """
    Resolve active plan row by (user_id, name, is_active=true).
    Returns full row including id, plan_json, etc.
    """
    try:
        resp = (
            supabase.table("validation_test_plans")
            .select("*")
            .eq("user_id", str(user_id))
            .eq("name", str(test_plan_name))
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception:
        return None
