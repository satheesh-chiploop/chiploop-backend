# backend/services/validation_apply_proposal_service.py

from __future__ import annotations

import os
import json
import datetime
import hashlib
from typing import Any, Dict, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")


def _utc_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _normalize_storage_path(p: str) -> str:
    p = (p or "").strip()
    if p.startswith("/artifacts/anonymous/"):
        return p[len("/artifacts/anonymous/") :]
    if p.startswith("/artifacts/"):
        return p[len("/artifacts/") :]
    if p.startswith("/"):
        return p[1:]
    return p


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _get_metadata(plan_json: Dict[str, Any]) -> Dict[str, Any]:
    md = plan_json.get("metadata")
    return md if isinstance(md, dict) else {}


def _set_metadata(plan_json: Dict[str, Any], md: Dict[str, Any]) -> None:
    plan_json["metadata"] = md


def _hash_plan(plan_json: Dict[str, Any]) -> str:
    blob = json.dumps(plan_json, sort_keys=True, default=str).encode("utf-8", errors="ignore")
    return hashlib.sha256(blob).hexdigest()


def _load_plan_row_by_id(supabase, plan_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = (
            supabase.table("validation_test_plans")
            .select("*")
            .eq("id", plan_id)
            .single()
            .execute()
        )
        return resp.data or None
    except Exception:
        return None


def _load_active_plan_row_by_name(supabase, user_id: str, plan_name: str) -> Optional[Dict[str, Any]]:
    """
    MVP: resolve base plan id using user_id + test_plan_name.
    Picks the most recently created active plan.
    """
    try:
        resp = (
            supabase.table("validation_test_plans")
            .select("*")
            .eq("user_id", str(user_id))
            .eq("name", str(plan_name))
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _download_json_artifact(supabase, storage_key: str) -> Dict[str, Any]:
    """
    storage_key must be a key in ARTIFACT_BUCKET, e.g.
    backend/workflows/<wf_id>/validation/proposed_test_plan.json
    """
    key = _normalize_storage_path(storage_key)
    blob = supabase.storage.from_(ARTIFACT_BUCKET).download(key)
    if isinstance(blob, (bytes, bytearray)):
        txt = blob.decode("utf-8", errors="ignore")
    else:
        txt = str(blob)
    obj = json.loads(txt)
    if not isinstance(obj, dict):
        raise ValueError("Proposal artifact is not a JSON object")
    return obj


def _extract_proposed_plan(state: Dict[str, Any], supabase) -> Tuple[Dict[str, Any], str]:
    """
    Returns: (proposed_plan_json, source_artifact_path)
    Accepts:
      - state["proposed_test_plan"] as dict
      - state["proposed_plan_json"] as dict
      - state["proposal_artifact_path"] (storage key)
      - state["source_artifact_path"] (storage key)
    """
    for k in ("proposed_test_plan", "proposed_plan_json"):
        v = state.get(k)
        if isinstance(v, dict) and v:
            return v, (state.get("proposal_artifact_path") or state.get("source_artifact_path") or "")

    artifact_path = (
        state.get("proposal_artifact_path")
        or state.get("source_artifact_path")
        or state.get("proposed_test_plan_artifact_path")
        or ""
    )
    artifact_path = (artifact_path or "").strip()
    if not artifact_path:
        raise ValueError("Missing proposed plan (no JSON in state and no proposal_artifact_path)")

    proposed = _download_json_artifact(supabase, artifact_path)
    return proposed, artifact_path


def _validate_plan_schema(plan_json: Dict[str, Any]) -> None:
    """
    Keep MVP validation minimal & strict:
    - must be dict
    - must contain 'dut' and 'tests'
    - tests must be list and non-empty
    """
    if not isinstance(plan_json, dict):
        raise ValueError("plan_json is not an object")
    if "dut" not in plan_json or "tests" not in plan_json:
        raise ValueError("plan_json missing required keys: dut/tests")
    tests = plan_json.get("tests")
    if not isinstance(tests, list) or len(tests) == 0:
        raise ValueError("plan_json.tests must be a non-empty list")


def apply_test_plan_proposal(state: Dict[str, Any], supabase) -> Dict[str, Any]:
    """
    WF9 service (deterministic):
    - resolve base plan row (by id OR by user_id+test_plan_name)
    - load proposed plan (from artifact or state)
    - stamp metadata: version/parent/status/origin
    - deactivate previous active plans with same name (for this user)
    - insert new plan row as active
    - write apply_status.md artifact
    """
    now = _utc_iso()

    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    base_plan_id = state.get("base_test_plan_id") or state.get("test_plan_id")
    test_plan_name = (
        state.get("test_plan_name")
        or state.get("validation_test_plan_name")
        or state.get("test_plan")
        or ""
    ).strip()
    proposal_kind = (state.get("proposal_kind") or state.get("origin") or "proposal").strip().lower()

    if not workflow_id:
        state["status"] = "❌ Apply Proposal: missing workflow_id"
        return state
    if not user_id:
        msg = "❌ Apply Proposal: missing user_id"
        save_text_artifact_and_record(workflow_id, "Validation Apply Proposal Agent", "validation", "apply_status.md",
                                      f"## Apply Proposal\n\n- Time: `{now}`\n\n{msg}\n")
        state["status"] = msg
        return state

    # ✅ MVP: allow resolving base plan by name
    base_row = None
    if base_plan_id:
        base_row = _load_plan_row_by_id(supabase, str(base_plan_id))
    else:
        if not test_plan_name:
            msg = "❌ Apply Proposal: missing base_test_plan_id AND test_plan_name"
            save_text_artifact_and_record(workflow_id, "Validation Apply Proposal Agent", "validation", "apply_status.md",
                                          f"## Apply Proposal\n\n- Time: `{now}`\n\n{msg}\n")
            state["status"] = msg
            return state
        base_row = _load_active_plan_row_by_name(supabase, str(user_id), test_plan_name)
        base_plan_id = (base_row or {}).get("id")

    if not base_row:
        msg = f"❌ Apply Proposal: base plan not found (id={base_plan_id} name='{test_plan_name}')"
        save_text_artifact_and_record(workflow_id, "Validation Apply Proposal Agent", "validation", "apply_status.md",
                                      f"## Apply Proposal\n\n- Time: `{now}`\n\n{msg}\n")
        state["status"] = msg
        return state

    base_name = base_row.get("name")
    base_desc = base_row.get("description")
    base_tags = base_row.get("tags")
    base_plan_json = base_row.get("plan_json") if isinstance(base_row.get("plan_json"), dict) else {}

    # Load proposed plan
    proposed_plan, source_artifact_path = _extract_proposed_plan(state, supabase)

    # Validate schema
    _validate_plan_schema(proposed_plan)

    # Stamp metadata (no DB schema change)
    base_md = _get_metadata(base_plan_json)
    base_version = _safe_int(base_md.get("version"), 1)
    new_version = base_version + 1

    new_md = dict(_get_metadata(proposed_plan))
    new_md["version"] = new_version
    new_md["parent_plan_id"] = str(base_row.get("id"))
    new_md["status"] = new_md.get("status") or "learning"
    new_md["origin"] = proposal_kind
    new_md["applied_from_workflow_id"] = str(workflow_id)
    new_md["applied_at"] = now
    new_md["base_plan_hash"] = _hash_plan(base_plan_json) if isinstance(base_plan_json, dict) else ""
    new_md["proposed_plan_hash"] = _hash_plan(proposed_plan)
    if source_artifact_path:
        new_md["source_artifact_path"] = source_artifact_path

    _set_metadata(proposed_plan, new_md)

    # Deactivate other active plans of same name for this user
    try:
        (
            supabase.table("validation_test_plans")
            .update({"is_active": False})
            .eq("user_id", str(user_id))
            .eq("name", str(base_name))
            .eq("is_active", True)
            .execute()
        )
    except Exception:
        pass

    # Insert new plan row
    insert_payload = {
        "user_id": str(user_id),
        "name": str(base_name),
        "description": base_desc,
        "plan_json": proposed_plan,
        "source_workflow_id": str(workflow_id),
        "source_artifact_path": source_artifact_path or None,
        "tags": base_tags,
        "is_active": True,
    }

    try:
        ins = supabase.table("validation_test_plans").insert(insert_payload).execute()
        new_row = (ins.data or [None])[0] if isinstance(ins.data, list) else ins.data
        new_plan_id = (new_row or {}).get("id")
    except Exception as e:
        msg = f"❌ Apply Proposal failed to insert new plan: {type(e).__name__}: {e}"
        save_text_artifact_and_record(workflow_id, "Validation Apply Proposal Agent", "validation", "apply_status.md",
                                      f"## Apply Proposal\n\n- Time: `{now}`\n\n{msg}\n")
        state["status"] = msg
        return state

    content = (
        "## Apply Proposal\n\n"
        f"- Time: `{now}`\n"
        f"- User: `{user_id}`\n"
        f"- Base plan id: `{base_plan_id}`\n"
        f"- Base name: `{base_name}`\n"
        f"- New version: `{new_version}`\n"
        f"- New plan id: `{new_plan_id}`\n"
        f"- Origin: `{proposal_kind}`\n"
        f"- Source artifact: `{source_artifact_path or ''}`\n\n"
        "✅ Proposal applied: new test plan version is now active.\n"
    )
    save_text_artifact_and_record(workflow_id, "Validation Apply Proposal Agent", "validation", "apply_status.md", content)

    state["status"] = "✅ Apply Proposal: success"
    state["applied_plan_id"] = new_plan_id
    state["applied_plan_version"] = new_version
    state["applied_plan_name"] = base_name
    state["execution_mode"] = state.get("execution_mode") or "delta"
    return state
