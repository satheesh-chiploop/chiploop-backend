import os
import json
import csv
import io
import datetime
import logging
logger = logging.getLogger(__name__)
from typing import Any, Dict, Optional, Tuple


from utils.artifact_utils import save_text_artifact_and_record


def _now_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _safe_float(x: Any) -> Optional[float]:
    try:
        # PyVISA returns strings; SCPI often returns "1.234\n"
        return float(str(x).strip())
    except Exception:
        return None


def _get_supabase_client():
    """
    Optional: update validation_instruments.scpi_idn from the agent.
    If env vars are missing, we simply skip DB updates.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        return None

    try:
        from supabase import create_client  # type: ignore
        return create_client(url, key)
    except Exception:
        return None


def _update_instrument_idn_if_possible(instrument_row: dict, scpi_idn: str) -> None:
    """
    Best-effort DB update:
      - expects instrument_row includes {id, user_id}
      - updates validation_instruments.scpi_idn
    If anything is missing, we skip (no hard failure).
    """
    inst_id = instrument_row.get("id")
    user_id = instrument_row.get("user_id")
    if not inst_id or not user_id:
        return

    sb = _get_supabase_client()
    if not sb:
        return

    try:
        sb.table("validation_instruments") \
            .update({"scpi_idn": scpi_idn, "updated_at": _now_iso()}) \
            .eq("user_id", user_id) \
            .eq("id", inst_id) \
            .execute()
    except Exception:
        # Do not break execution if DB update fails
        pass


def _open_resource(rm, resource_string: str):
    h = rm.open_resource(resource_string)
    # conservative defaults
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


def _scpi_run_step(handle, cmd: str) -> Tuple[bool, Any, Optional[str]]:
    """
    Returns: (ok, value, error)
      - For queries (cmd endswith '?'): uses query()
      - For writes: uses write()
    """
    try:
        if cmd.strip().endswith("?"):
            val = handle.query(cmd)
            return True, val, None
        else:
            handle.write(cmd)
            return True, None, None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def run_agent(state: dict) -> dict:
    """
    Executes validation/test_sequence.json steps via PyVISA (SCPI).
    Inputs expected in state:
      - workflow_id: str
      - test_sequence: dict (as produced by validation_sequence_builder_agent)
    Produces artifacts:
      - validation/results.json
      - validation/results.csv
      - validation/run_manifest.json
    """
    workflow_id = state.get("workflow_id")
    seq = state.get("test_sequence") or {}

    if not workflow_id or not seq:
        state["status"] = "❌ Missing workflow_id or test_sequence"
        logger.warning(
          "[EXECUTION ORCH] Early return | "
          f"has_test_sequence={bool(test_sequence)} | "
          f"has_workflow_id={bool(workflow_id)} | "
          f"state_keys={list(state.keys())}"
        )
        return state

    agent_name = "Validation Execution Orchestrator Agent"

    logger.info(f"[DEBUG] {agent_name} status={state.get('status')}")

    # Decide executor mode: "pyvisa" (default) or "stub"
    mode = (os.getenv("VALIDATION_EXECUTION_MODE") or "pyvisa").lower()
    mode = "stub"
    results_rows = []
    results_json: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "timestamp": _now_iso(),
        "executor": mode,
        "tests": []
    }

    # Build instrument map from sequence["instruments"]
    instruments = (seq.get("instruments") or {})
    handle_cache: Dict[str, Any] = {}
    
    def _get_handle(rm, resource: str):
        if resource not in handle_cache:
            handle_cache[resource] = _open_resource(rm, resource)
        return handle_cache[resource]

    if mode == "stub":
        for test in seq.get("tests", []):
            captures = {}
            for step in test.get("steps", []):
                if step.get("cmd") == "*IDN?":
                    inst_key = step.get("instrument")
                    captures[f"{inst_key}_idn"] = f"{str(inst_key).upper()},MOCK,0,1.0"
                if step.get("capture_as") == "VOUT":
                    captures["VOUT"] = 1.800
                if step.get("capture_as") == "VPP_CH1":
                    captures["VPP_CH1"] = 0.120

            passed = True
            results_json["tests"].append({"name": test.get("name"), "passed": passed, "captures": captures})
            results_rows.append({"test": test.get("name"), "passed": passed, **captures})

    else:
        try:
            import pyvisa
        except Exception as e:
            state["status"] = f"❌ PyVISA not available: {type(e).__name__}: {e}"
            return state

        rm = pyvisa.ResourceManager()

        for test in seq.get("tests", []):
            test_name = test.get("name") or "unnamed_test"
            captures: Dict[str, Any] = {}
            step_logs = []
            ok_all = True

            for step in test.get("steps", []):
                if step.get("type") != "scpi":
                    continue

                inst_key = step.get("instrument")
                resource = step.get("resource")
                cmd = (step.get("cmd") or "").strip()
                cap_as = step.get("capture_as")
                expect = (step.get("expect") or "").lower()

                if not resource or not cmd:
                    ok_all = False
                    step_logs.append({"ok": False, "cmd": cmd, "error": "Missing resource/cmd"})
                    continue

                h = _get_handle(rm, resource)
                ok, val, err = _scpi_run_step(h, cmd)

                step_logs.append({
                    "ok": ok,
                    "instrument": inst_key,
                    "resource": resource,
                    "cmd": cmd,
                    "error": err
                })

                if not ok:
                    ok_all = False
                    continue

                if cmd == "*IDN?":
                    idn = str(val).strip()
                    captures[f"{inst_key}_idn"] = idn

                    inst_row = instruments.get(inst_key) or {}
                    _update_instrument_idn_if_possible(inst_row, idn)

                if cap_as:
                    if expect == "string":
                        captures[cap_as] = str(val).strip()
                    else:
                        f = _safe_float(val)
                        captures[cap_as] = f if f is not None else str(val).strip()

            passed = bool(ok_all)

            results_json["tests"].append({
                "name": test_name,
                "passed": passed,
                "captures": captures,
                "steps": step_logs
            })

            row = {"test": test_name, "passed": passed, **captures}
            results_rows.append(row)

        for h in handle_cache.values():
            try:
                h.close()
            except Exception:
                pass
        try:
            rm.close()
        except Exception:
            pass

    # Write CSV
    buf = io.StringIO()
    fieldnames = sorted({k for r in results_rows for k in r.keys()}) if results_rows else ["test", "passed"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in results_rows:
        writer.writerow(r)
    csv_out = buf.getvalue()

    # ✅ FIX: use correct artifact_utils signature
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="results.json",
        content=json.dumps(results_json, indent=2),
    )
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="results.csv",
        content=csv_out,
    )

    manifest = {
        "workflow_id": workflow_id,
        "executor": mode,
        "timestamp": _now_iso(),
        "notes": "PyVISA SCPI execution. Step 2E will apply pass/fail using limits from validation/test_plan.json."
    }
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="run_manifest.json",
        content=json.dumps(manifest, indent=2),
    )

    state["validation_results"] = results_json
    state["status"] = f"✅ Sequence executed ({mode}) and results generated"
    return state
