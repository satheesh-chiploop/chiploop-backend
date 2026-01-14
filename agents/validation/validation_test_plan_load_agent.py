# agents/validation/validation_test_plan_load_agent.py
#
# Validation Test Plan Load Agent
# - Loads a previously saved test plan from Supabase table `validation_test_plans`
# - Populates state["test_plan"] so downstream agents can run without requiring spec
#
# Artifacts:
#   validation/test_plan_loaded.json
#   validation/test_plan_loaded_summary.md
#
# Expected state inputs:
#   - workflow_id: str (required)
#   - user_id: str (required)
#   - test_plan_id: str (required)  # UUID
#
# Outputs:
#   - state["test_plan"]: dict
#   - state["test_plan_meta"]: dict (id/name/created_at/tags)
#
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from supabase import create_client
from utils.artifact_utils import save_text_artifact_and_record

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    # Keep import-time failures explicit (matches your other agents' expectations)
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY env vars")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _require_str(state: Dict[str, Any], key: str) -> Optional[str]:
    v = state.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    agent_name = "Validation Test Plan Load Agent"

    workflow_id = _require_str(state, "workflow_id")
    user_id = _require_str(state, "user_id")
    test_plan_id = _require_str(state, "test_plan_id")

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state
    if not user_id:
        state["status"] = "❌ Missing user_id"
        return state
    if not test_plan_id:
        state["status"] = "❌ Missing test_plan_id"
        return state

    # Fetch plan row
    rows = (
        supabase.table("validation_test_plans")
        .select("id,user_id,name,description,plan_json,tags,is_active,created_at,updated_at,source_workflow_id,source_artifact_path")
        .eq("id", test_plan_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
    )

    if not rows:
        state["status"] = f"❌ Test plan not found for user: {test_plan_id}"
        return state

    row = rows[0]
    if not row.get("is_active", True):
        state["status"] = f"❌ Test plan is inactive: {test_plan_id}"
        return state

    plan = row.get("plan_json") or {}
    if not isinstance(plan, dict) or not plan:
        state["status"] = f"❌ Test plan JSON is empty/invalid: {test_plan_id}"
        return state

    # Update state for downstream agents
    state["test_plan"] = plan
    state["test_plan_meta"] = {
        "id": row.get("id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "tags": row.get("tags") or [],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "source_workflow_id": row.get("source_workflow_id"),
        "source_artifact_path": row.get("source_artifact_path"),
    }

    # Artifacts
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="test_plan_loaded.json",
        content=json.dumps(
            {
                "test_plan_id": test_plan_id,
                "meta": state["test_plan_meta"],
                "test_plan": plan,
            },
            indent=2,
        ),
    )

    md_lines = [
        "# Test Plan Loaded",
        "",
        f"- Test plan id: `{test_plan_id}`",
        f"- Name: **{row.get('name') or '(unnamed)'}**",
        f"- Active: `{row.get('is_active', True)}`",
        f"- Tags: {', '.join(row.get('tags') or []) or '(none)'}",
        f"- Created: `{row.get('created_at')}`",
        "",
        "This plan is now available in `state['test_plan']` for downstream execution.",
        "",
    ]
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="test_plan_loaded_summary.md",
        content="\n".join(md_lines),
    )

    state["status"] = f"✅ Loaded test plan: {row.get('name') or test_plan_id}"
    return state
