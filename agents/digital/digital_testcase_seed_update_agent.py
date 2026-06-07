import json
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Testcase Seed Update Agent"
EXECUTABLE_TESTS = ["smoke_test", "constrained_random_sanity"]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _selection(state: Dict[str, Any]) -> str:
    value = str(state.get("random_vs_directed") or "both").strip().lower()
    return value if value in {"directed", "random", "both"} else "both"


def _allowed_tests(selection: str) -> List[str]:
    if selection == "directed":
        return ["smoke_test"]
    if selection == "random":
        return ["constrained_random_sanity"]
    return list(EXECUTABLE_TESTS)


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    iteration = int(state.get("closure_iteration_index") or 1)
    out_dir = workflow_dir / "verify_closure" / f"iteration_{iteration:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    gap = state.get("coverage_gap_analysis") if isinstance(state.get("coverage_gap_analysis"), dict) else {}
    triage = state.get("failure_triage") if isinstance(state.get("failure_triage"), dict) else {}
    added_points = state.get("closure_added_coverage_points") if isinstance(state.get("closure_added_coverage_points"), list) else []
    failures = [f for f in (triage.get("failures") or []) if isinstance(f, dict)]
    selection = _selection(state)
    allowed_tests = _allowed_tests(selection)
    rerun_mode = str(state.get("rerun_mode") or "coverage_targeted")

    testcase_intents: List[Dict[str, Any]] = []
    for idx, point in enumerate(added_points[:12], start=1):
        source_gap_type = str(point.get("source_gap_type") or "")
        mapped_test = "smoke_test" if selection == "directed" else "constrained_random_sanity"
        if selection == "both" and source_gap_type in {"functional_bin_gap", "functional_coverage_below_target"}:
            mapped_test = "smoke_test"
        testcase_intents.append({
            "name": f"closure_cov_{iteration:03d}_{idx:03d}",
            "kind": "code_coverage_intent" if source_gap_type.startswith("code_") else "coverage_directed_intent",
            "source_gap_type": source_gap_type,
            "source": point.get("source"),
            "executable": mapped_test in allowed_tests,
            "mapped_executable_test": mapped_test if mapped_test in allowed_tests else allowed_tests[0],
            "coverage_point": point.get("coverage_point"),
            "traceability_id": point.get("id"),
        })
    for idx, failure in enumerate(failures[:12], start=1):
        testcase = str(failure.get("testcase") or "constrained_random_sanity")
        testcase_intents.append({
            "name": f"replay_{testcase}_seed_{failure.get('seed')}",
            "kind": "failure_replay",
            "executable": testcase in allowed_tests,
            "mapped_executable_test": testcase if testcase in allowed_tests else allowed_tests[0],
            "seed": failure.get("seed"),
            "traceability": failure,
        })

    seed_budget = max(_int(state.get("seed_budget"), _int(state.get("seed_count"), 5)), 1)
    failed_seeds = []
    for failure in failures:
        try:
            failed_seeds.append(int(failure.get("seed")))
        except Exception:
            pass
    if rerun_mode == "failed_only":
        seed_plan = failed_seeds or [1]
    else:
        seed_plan = list(dict.fromkeys(failed_seeds + list(range(1, seed_budget + 1))))

    if rerun_mode == "failed_only" and failures:
        executable_tests = list(dict.fromkeys(str(t.get("mapped_executable_test")) for t in testcase_intents if t.get("kind") == "failure_replay"))
    elif rerun_mode == "full_regression":
        executable_tests = allowed_tests
    else:
        executable_tests = list(dict.fromkeys(str(t.get("mapped_executable_test")) for t in testcase_intents if t.get("mapped_executable_test")))
    executable_tests = [t for t in executable_tests if t in allowed_tests] or allowed_tests

    report = {
        "type": "testcase_seed_update",
        "iteration": iteration,
        "rerun_mode": rerun_mode,
        "random_vs_directed": selection,
        "testcase_intents_added": len(testcase_intents),
        "executable_tests": executable_tests,
        "testcase_intents": testcase_intents,
        "seeds_added": len(seed_plan),
        "simulation_seeds": seed_plan,
        "note": "Closure testcase intents are traceable. Rerun executes only tests with generated Cocotb functions.",
    }
    report_txt = json.dumps(report, indent=2)
    md = "\n".join([
        "# Testcase And Seed Update",
        "",
        f"- Iteration: {iteration}",
        f"- Testcase intents added: {len(testcase_intents)}",
        f"- Executable tests for rerun: {', '.join(executable_tests)}",
        f"- Seeds: {', '.join(map(str, seed_plan))}",
        "",
        "## Testcase Intents",
        *[f"- `{item['name']}` -> `{item['mapped_executable_test']}` ({item['kind']})" for item in testcase_intents],
        "",
    ])
    (out_dir / "testcase_seed_update.json").write_text(report_txt, encoding="utf-8")
    (out_dir / "testcase_seed_update.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "testcase_seed_update.json", report_txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "testcase_seed_update.md", md)

    state["closure_testcase_seed_update"] = report
    state["closure_testcase_intents"] = testcase_intents
    state["vv_testcases"] = executable_tests
    state["testcases"] = executable_tests
    state["simulation_seeds"] = seed_plan
    state["seed_count"] = len(seed_plan)
    return state
