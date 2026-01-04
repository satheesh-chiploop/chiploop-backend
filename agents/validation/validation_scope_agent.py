import json, datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _norm_list(x: Any) -> List[str]:
    if not x:
        return []
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        return [str(i) for i in x if i is not None]
    return []


def _match_any_tag(test_tags: List[str], wanted: List[str]) -> bool:
    if not wanted:
        return True
    s = {t.strip().lower() for t in (test_tags or [])}
    for w in wanted:
        if w.strip().lower() in s:
            return True
    return False


def run_agent(state: dict) -> dict:
    """
    Validation Scope Agent (between Test Plan and Sequence Builder)

    Inputs:
      - workflow_id: str
      - test_plan: dict (required)
      - scope: optional dict controlling selection, supports:
          {
            "mode": "all" | "by_test_names" | "by_tags",
            "include_tests": ["Test A", ...],
            "exclude_tests": ["Test B", ...],
            "include_tags": ["smoke", "dc"],
            "exclude_tags": ["noise"],
          }

    Outputs:
      - validation/scope_selection.json
      - validation/scoped_test_plan.json

    State:
      - state["scoped_test_plan"] = filtered plan
    """
    workflow_id = state.get("workflow_id")
    plan = state.get("test_plan") or state.get("validation_test_plan") or {}
    scope = state.get("scope") or {}

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    if not plan or not isinstance(plan, dict) or not plan.get("tests"):
        state["status"] = "❌ Missing test_plan in state (expected state['test_plan'])"
        return state

    # Defaults: include everything
    mode = (scope.get("mode") or "all").strip().lower()

    include_tests = [t.strip() for t in _norm_list(scope.get("include_tests"))]
    exclude_tests = [t.strip() for t in _norm_list(scope.get("exclude_tests"))]
    include_tags = [t.strip() for t in _norm_list(scope.get("include_tags"))]
    exclude_tags = [t.strip() for t in _norm_list(scope.get("exclude_tags"))]

    selected = []
    for t in (plan.get("tests") or []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        tags = [str(x) for x in (t.get("tags") or [])]

        # Exclude by name
        if name and name in exclude_tests:
            continue

        # Exclude by tag
        if exclude_tags and _match_any_tag(tags, exclude_tags):
            continue

        if mode == "all":
            selected.append(t)
            continue

        if mode == "by_test_names":
            if not include_tests:
                # If mode asks for names but none provided, select none (explicit)
                continue
            if name in include_tests:
                selected.append(t)
            continue

        if mode == "by_tags":
            # If include_tags empty, treat as select all (within exclusions)
            if _match_any_tag(tags, include_tags):
                selected.append(t)
            continue

        # Unknown mode → safe default: keep all
        selected.append(t)

    scoped_plan = dict(plan)
    scoped_plan["scoped_at"] = _now_iso()
    scoped_plan["scope"] = {
        "mode": mode,
        "include_tests": include_tests,
        "exclude_tests": exclude_tests,
        "include_tags": include_tags,
        "exclude_tags": exclude_tags,
        "tests_before": len(plan.get("tests") or []),
        "tests_after": len(selected),
    }
    scoped_plan["tests"] = selected

    # Save artifacts
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        rel_path="validation/scope_selection.json",
        content=json.dumps(scoped_plan["scope"], indent=2),
        content_type="application/json",
    )

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        rel_path="validation/scoped_test_plan.json",
        content=json.dumps(scoped_plan, indent=2),
        content_type="application/json",
    )

    state["scoped_test_plan"] = scoped_plan
    state["status"] = f"✅ Scope applied: {scoped_plan['scope']['tests_after']}/{scoped_plan['scope']['tests_before']} tests selected"
    return state
