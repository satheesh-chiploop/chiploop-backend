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


def run_agent(state: dict) -> dict:
    """
    Validation Test Plan Load Agent

    Supports loading a test plan by:
      - state["test_plan_id"]  (preferred, exact)
      - state["test_plan_name"] (UX-friendly, resolved per-user; errors on duplicates)

    Outputs:
      - state["test_plan"]      : dict (plan_json)
      - state["test_plan_id"]   : str (resolved id if name used)
      - state["test_plan_meta"] : dict (id, name, description, etc.)
      - state["status"]         : "✅ ..." or "❌ ..."
    """
    logger = state.get("logger")
    if logger is None:
        import logging
        logger = logging.getLogger(__name__)

    def _s(v):
        return (v or "").strip() if isinstance(v, str) else v

    user_id = _s(state.get("user_id"))
    workflow_id = _s(state.get("workflow_id"))
    test_plan_id = _s(state.get("test_plan_id"))
    test_plan_name = _s(state.get("test_plan_name"))

    if not supabase:
        state["status"] = "❌ Supabase client not configured"
        return state

    if not user_id:
        state["status"] = "❌ Missing user_id"
        return state

    if not test_plan_id and not test_plan_name:
        state["status"] = "❌ Missing test_plan_id or test_plan_name"
        return state

    try:
        base_q = (
            supabase.table("validation_test_plans")
            .select(
                "id,user_id,name,description,plan_json,tags,is_active,created_at,updated_at,"
                "source_workflow_id,source_artifact_path"
            )
            .eq("user_id", user_id)
            .eq("is_active", True)
        )

        # ---- Path A: load by ID (exact) ----
        if test_plan_id:
            rows = base_q.eq("id", test_plan_id).limit(1).execute().data or []
            if not rows:
                state["status"] = f"❌ Test plan not found for user by id: {test_plan_id}"
                return state
            row = rows[0]

        # ---- Path B: load by NAME (UX friendly) ----
        else:
            rows = (
                base_q.eq("name", test_plan_name)
                .order("created_at", desc=True)
                .execute()
                .data
                or []
            )
            if not rows:
                state["status"] = f"❌ Test plan not found for user by name: {test_plan_name}"
                return state

            # If you prefer "pick latest", you can delete this duplicate check and use rows[0].
            if len(rows) > 1:
                example_ids = [r.get("id") for r in rows[:5]]
                state["status"] = (
                    f"❌ Multiple active test plans found with name '{test_plan_name}'. "
                    f"Use test_plan_id instead. Example IDs: {example_ids}"
                )
                return state

            row = rows[0]
            test_plan_id = row.get("id")
            state["test_plan_id"] = test_plan_id  # resolved id

        plan = row.get("plan_json") or {}
        if not isinstance(plan, dict) or not plan:
            state["status"] = "❌ Loaded test plan is empty or invalid plan_json"
            return state

        # Populate state outputs
        state["test_plan"] = plan
        state["test_plan_meta"] = {
            "id": row.get("id"),
            "name": row.get("name"),
            "description": row.get("description"),
            "tags": row.get("tags"),
            "source_workflow_id": row.get("source_workflow_id"),
            "source_artifact_path": row.get("source_artifact_path"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

        # Optional: lightweight artifact for traceability (only if workflow_id exists)

        # Optional: artifacts for traceability (only if workflow_id exists)
        if workflow_id:
            try:
                # 1) Save the loaded plan itself (so ZIP always contains the plan used in this run)
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name="Validation Test Plan Load Agent",
                    subdir="validation",
                    filename="test_plan_loaded.json",
                    content=json.dumps(plan, indent=2),
                )

                # 2) Save metadata (already doing)
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name="Validation Test Plan Load Agent",
                    subdir="validation",
                    filename="test_plan_loaded_meta.json",
                    content=json.dumps(state["test_plan_meta"], indent=2),
                )

                # 3) Tiny human-readable summary (optional but useful when debugging runs)
                summary_md = f"""# Test Plan Loaded

        - **id**: {state["test_plan_meta"].get("id")}
        - **name**: {state["test_plan_meta"].get("name")}
        - **active**: {row.get("is_active")}
        - **tests**: {len(plan.get("tests", []) or [])}
        - **source_workflow_id**: {state["test_plan_meta"].get("source_workflow_id")}
        - **source_artifact_path**: {state["test_plan_meta"].get("source_artifact_path")}
        """
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name="Validation Test Plan Load Agent",
                    subdir="validation",
                    filename="test_plan_loaded_summary.md",
                    content=summary_md,
                )
            except Exception as e:
                logger.warning(f"[TEST PLAN LOAD] Could not write artifacts: {type(e).__name__}: {e}")

        state["status"] = "✅ Validation Test Plan loaded"
        return state

    except Exception as e:
        logger.exception("Validation Test Plan Load Agent failed")
        state["status"] = f"❌ Validation Test Plan Load Agent failed: {type(e).__name__}: {e}"
        return state
