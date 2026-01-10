# agents/validation/validation_preflight_agent.py
#
# Validation Preflight Agent
# - Safe, bench-readiness checks only (no DUT stimulus)
# - Optionally performs SCPI *IDN? queries (pyvisa mode) OR deterministic stub responses
# - Validates instrument coverage vs test_plan requirements (psu/dmm/scope)
#
# Artifacts:
#   validation/preflight_report.json
#   validation/preflight_summary.md
#
# Expected state inputs:
#   - workflow_id: str (required)
#   - user_id: str (required)
#   - bench_id: str (recommended for Use Case 1)
#   - bench_setup: dict (optional; if absent we try to use state["bench_setup"] from Instrument Setup Agent)
#   - test_plan / scoped_test_plan: dict (optional but recommended for coverage checks)
#
# Notes:
# - This agent does NOT write to the database.
# - This agent is safe to run frequently.
#
import os
import json
import datetime
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _norm_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _mode() -> str:
    # your execution orchestrator currently forces stub; we won't.
    return (os.getenv("VALIDATION_EXECUTION_MODE") or "pyvisa").strip().lower()


def _safe_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() in ["1", "true", "yes", "y", "on"]
    return bool(x)


def _pick_instrument_by_type(bench_setup: Dict[str, Any], inst_type: str) -> Optional[Dict[str, Any]]:
    for inst in (bench_setup or {}).get("instruments", []) or []:
        if (inst or {}).get("instrument_type") == inst_type:
            return inst
    return None


def _required_types_from_plan(plan: Dict[str, Any]) -> Set[str]:
    """
    Union required instrument types across all tests in plan.
    Expects plan.tests[].required_instruments = ["psu","dmm","scope",...]
    """
    needed: Set[str] = set()
    for t in _as_list((plan or {}).get("tests")):
        if not isinstance(t, dict):
            continue
        for it in _as_list(t.get("required_instruments")):
            s = _norm_str(it).lower()
            if s:
                needed.add(s)
    return needed


def _available_types_from_bench(bench_setup: Dict[str, Any]) -> Set[str]:
    have: Set[str] = set()
    for inst in (bench_setup or {}).get("instruments", []) or []:
        t = _norm_str((inst or {}).get("instrument_type")).lower()
        if t:
            have.add(t)
    return have


def _get_pyvisa_rm():
    try:
        import pyvisa  # type: ignore
    except Exception:
        return None, "pyvisa_not_installed"

    try:
        backend = os.getenv("PYVISA_BACKEND")  # optional, e.g. "@py"
        rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
        return rm, None
    except Exception as e:
        return None, f"pyvisa_rm_error: {type(e).__name__}: {e}"


def _open_resource(rm, resource_string: str):
    h = rm.open_resource(resource_string)
    try:
        h.timeout = int(os.getenv("PYVISA_TIMEOUT_MS", "5000"))
    except Exception:
        pass
    try:
        h.read_termination = "\n"
        h.write_termination = "\n"
    except Exception:
        pass
    return h


def _scpi_idn(handle) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        val = handle.query("*IDN?")
        return True, _norm_str(val), None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def _mk_summary_md(report: Dict[str, Any]) -> str:
    s = report.get("summary") or {}
    checks = report.get("checks") or []

    lines: List[str] = []
    lines.append("# Preflight Summary")
    lines.append("")
    lines.append(f"- Workflow: `{report.get('workflow_id')}`")
    if report.get("bench_id"):
        lines.append(f"- Bench: `{report.get('bench_id')}`")
    lines.append(f"- Time: {report.get('timestamp')}")
    lines.append(f"- Mode: **{report.get('mode')}**")
    lines.append("")
    lines.append("## Result")
    lines.append(f"- Status: **{s.get('status')}**")
    lines.append(f"- Checks: {s.get('checks_passed',0)} passed / {s.get('checks_failed',0)} failed / {s.get('checks_warn',0)} warnings")
    lines.append("")

    # Coverage
    cov = report.get("coverage") or {}
    if cov:
        lines.append("## Instrument Coverage")
        lines.append(f"- Needed: {', '.join(cov.get('needed', []) or []) or '(none)'}")
        lines.append(f"- Present: {', '.join(cov.get('present', []) or []) or '(none)'}")
        miss = cov.get("missing") or []
        if miss:
            lines.append(f"- Missing: **{', '.join(miss)}**")
        else:
            lines.append("- Missing: (none)")
        lines.append("")

    # Top issues
    failed = [c for c in checks if (c.get("result") == "fail")]
    warned = [c for c in checks if (c.get("result") == "warn")]

    if failed:
        lines.append("## Failures")
        for c in failed[:10]:
            lines.append(f"- **{c.get('check')}**: {c.get('message')}")
        lines.append("")

    if warned:
        lines.append("## Warnings")
        for c in warned[:10]:
            lines.append(f"- **{c.get('check')}**: {c.get('message')}")
        lines.append("")

    lines.append("## Next Step")
    if s.get("status") == "PASS":
        lines.append("- Preflight is green. You can run **Run Validation** workflow.")
    else:
        lines.append("- Fix failures above, then re-run **Preflight Bench**.")

    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    bench_id = state.get("bench_id")

    plan = state.get("scoped_test_plan") or state.get("test_plan") or state.get("validation_test_plan") or {}
    bench_setup = state.get("bench_setup") or {}

    if not workflow_id or not user_id:
        state["status"] = "❌ Missing workflow_id or user_id"
        return state

    agent_name = "Validation Preflight Agent"
    mode = _mode()

    report: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "bench_id": bench_id,
        "timestamp": _now_iso(),
        "mode": mode,
        "summary": {
            "status": "UNKNOWN",
            "checks_total": 0,
            "checks_passed": 0,
            "checks_failed": 0,
            "checks_warn": 0,
        },
        "coverage": {},
        "checks": [],
    }

    def add_check(check: str, result: str, message: str, details: Optional[Dict[str, Any]] = None):
        row = {"check": check, "result": result, "message": message}
        if details:
            row["details"] = details
        report["checks"].append(row)

    # Basic: bench_setup present
    if not bench_setup or not isinstance(bench_setup, dict) or not bench_setup.get("instruments"):
        add_check(
            "bench_setup_present",
            "fail",
            "bench_setup missing. Run 'Validation Instrument Setup Agent' earlier in the workflow (or provide bench_id mapping).",
            {"state_keys": list(state.keys())},
        )
    else:
        add_check("bench_setup_present", "pass", "bench_setup present.")

    # Basic: each instrument has required fields
    for inst in (bench_setup or {}).get("instruments", []) or []:
        itype = _norm_str(inst.get("instrument_type")).lower()
        nick = _norm_str(inst.get("nickname"))
        rid = _norm_str(inst.get("resource_string"))

        if not itype:
            add_check("instrument_type", "fail", f"Instrument missing instrument_type (nickname={nick or 'unknown'}).")
        if not nick:
            add_check("instrument_nickname", "warn", f"Instrument missing nickname (type={itype or 'unknown'}).")
        if not rid:
            add_check("instrument_resource_string", "fail", f"Instrument missing resource_string (nickname={nick or itype or 'unknown'}).")

    # Coverage vs plan
    if plan and isinstance(plan, dict) and plan.get("tests"):
        needed = sorted(list(_required_types_from_plan(plan)))
        present = sorted(list(_available_types_from_bench(bench_setup)))
        missing = sorted([x for x in needed if x not in set(present)])

        report["coverage"] = {"needed": needed, "present": present, "missing": missing}

        if missing:
            add_check(
                "instrument_coverage",
                "fail",
                f"Missing required instrument types: {', '.join(missing)}",
                {"needed": needed, "present": present},
            )
        else:
            add_check("instrument_coverage", "pass", "All required instrument types are present.")
    else:
        add_check(
            "plan_present_for_coverage",
            "warn",
            "No test_plan found in state; skipping coverage checks.",
        )

    # SCPI IDN checks (safe)
    instruments = (bench_setup or {}).get("instruments", []) or []
    if not instruments:
        add_check("scpi_idn", "fail", "No instruments available to query *IDN?.")
    else:
        if mode == "stub":
            # deterministic, safe
            for inst in instruments:
                itype = _norm_str(inst.get("instrument_type")).lower() or "instrument"
                nick = _norm_str(inst.get("nickname")) or itype
                add_check(
                    "scpi_idn",
                    "pass",
                    f"{nick}: MOCK_IDN ({itype.upper()},MOCK,0,1.0)",
                    {"instrument_type": itype, "resource_string": inst.get("resource_string")},
                )
        else:
            rm, rm_err = _get_pyvisa_rm()
            if rm_err or rm is None:
                add_check("pyvisa_resource_manager", "fail", f"Cannot initialize PyVISA ResourceManager: {rm_err}")
            else:
                for inst in instruments:
                    nick = _norm_str(inst.get("nickname")) or _norm_str(inst.get("instrument_type")) or "instrument"
                    resource = _norm_str(inst.get("resource_string"))
                    if not resource:
                        continue

                    try:
                        h = _open_resource(rm, resource)
                    except Exception as e:
                        add_check(
                            "open_resource",
                            "fail",
                            f"{nick}: failed to open resource {resource}",
                            {"error": f"{type(e).__name__}: {e}"},
                        )
                        continue

                    ok, idn, err = _scpi_idn(h)
                    if ok and idn:
                        add_check("scpi_idn", "pass", f"{nick}: {idn}", {"resource": resource})
                    else:
                        add_check("scpi_idn", "fail", f"{nick}: *IDN? failed", {"resource": resource, "error": err})

                    try:
                        h.close()
                    except Exception:
                        pass

                try:
                    rm.close()
                except Exception:
                    pass

    # Summarize
    report["summary"]["checks_total"] = len(report["checks"])
    report["summary"]["checks_passed"] = sum(1 for c in report["checks"] if c.get("result") == "pass")
    report["summary"]["checks_failed"] = sum(1 for c in report["checks"] if c.get("result") == "fail")
    report["summary"]["checks_warn"] = sum(1 for c in report["checks"] if c.get("result") == "warn")

    report["summary"]["status"] = "PASS" if report["summary"]["checks_failed"] == 0 else "FAIL"

    # Save artifacts
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="preflight_report.json",
        content=json.dumps(report, indent=2),
    )

    md = _mk_summary_md(report)
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="preflight_summary.md",
        content=md,
    )

    # Update state
    state["validation_preflight"] = report
    state["status"] = "✅ Preflight PASS" if report["summary"]["status"] == "PASS" else "❌ Preflight FAIL"
    return state
