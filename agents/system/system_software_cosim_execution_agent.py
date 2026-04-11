import datetime
import json
import os
import subprocess
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software CoSim Execution Agent"
OUTPUT_SUBDIR = "system/software_validation/cosim/execution"
REPORT_JSON = "system_cosim_execution_report.json"
SUMMARY_MD = "system_cosim_execution_summary.md"
DEBUG_JSON = "system_cosim_execution_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if isinstance(text, str) else ""


def _resolve_cwd(state: Dict[str, Any], scenario: Dict[str, Any]) -> str:
    # 1. scenario-specific cwd (highest priority)
    scenario_cwd = scenario.get("cwd")
    if isinstance(scenario_cwd, str) and scenario_cwd.strip() and os.path.isdir(scenario_cwd):
        return scenario_cwd

    # 2. software entry working dir (CRITICAL FIX)
    software_entry = (
        (state.get("system_software_cosim_harness_manifest") or {}).get("software_entry")
        or state.get("system_software_entry")
        or {}
    )

    entry_working_dir = str(software_entry.get("working_dir") or "").strip()
    validation_root = state.get("system_software_validation_local_root") or ""

    if entry_working_dir and validation_root:
        candidate = os.path.join(validation_root, entry_working_dir)
        if os.path.isdir(candidate):
            return candidate

    # 3. fallback → validation root (current behavior)
    if isinstance(validation_root, str) and validation_root.strip() and os.path.isdir(validation_root):
        return validation_root

    # 4. final fallback
    return os.getcwd()





def _run_cmd(cmd: List[str], cwd: str, timeout_sec: int) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        return {
            "returncode": result.returncode,
            "stdout_tail": _tail(result.stdout),
            "stderr_tail": _tail(result.stderr),
        }
    except Exception as exc:
        return {
            "returncode": -1,
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def _scenario_expectations(scenario: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "expected_events": scenario.get("expected_events") or [],
        "expected_registers": scenario.get("expected_registers") or {},
        "expected_interrupts": scenario.get("expected_interrupts") or [],
        "expected_signals": scenario.get("expected_signals") or [],
    }

def _llm_extract_observations(
    state: Dict[str, Any],
    scenario: Dict[str, Any],
    stdout_text: str,
) -> Dict[str, Any]:
    infer = state.get("llm_json_infer")
    if not callable(infer):
        return {}

    prompt = {
        "task": "Extract structured observations from software and simulation stdout.",
        "scenario": scenario,
        "stdout_text": stdout_text,
        "required_schema": {
            "observed_events": ["list[str]"],
            "observed_registers": {"example_register": "example_value"},
            "observed_interrupts": ["list[str]"],
            "observed_signals": ["list[str]"],
        },
    }

    try:
        result = infer(prompt)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _extract_observations(
    state: Dict[str, Any],
    scenario: Dict[str, Any],
    stdout_text: str,
) -> Dict[str, Any]:
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]

    observed_events = lines[:]
    observed_registers: Dict[str, Any] = {}
    observed_interrupts: List[str] = []
    observed_signals: List[str] = []

    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip().lower()
        v = value.strip()

        
        if k.startswith("register"):
            # Normalize register observation
            observed_registers[k] = v

        elif "interrupt" in k and v in {"1", "true", "asserted"}:
            # Normalize interrupt event
            observed_interrupts.append(k)

        elif k.startswith("signal") or "reset" in k:
            observed_signals.append(k)

    llm_obs = _llm_extract_observations(state, scenario, stdout_text)

    if isinstance(llm_obs.get("observed_events"), list):
        observed_events = llm_obs["observed_events"]
    if isinstance(llm_obs.get("observed_registers"), dict):
        observed_registers = llm_obs["observed_registers"]
    if isinstance(llm_obs.get("observed_interrupts"), list):
        observed_interrupts = llm_obs["observed_interrupts"]
    if isinstance(llm_obs.get("observed_signals"), list):
        observed_signals = llm_obs["observed_signals"]

    return {
        "observed_events": observed_events,
        "observed_registers": observed_registers,
        "observed_interrupts": observed_interrupts,
        "observed_signals": observed_signals,
    }


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    harness = state.get("system_software_cosim_harness_manifest") or {}
    harness_status = str(harness.get("harness_status") or "").strip().lower()

    if harness_status != "ready":
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "execution_status": "blocked",
            "message": "Harness is not ready for execution.",
            "blocked_dependencies": harness.get("blocked_dependencies") or [],
            "scenario_results": [],
        }
        _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record_text(workflow_id, SUMMARY_MD, "# System Software CoSim Execution Summary\n\n- Status: **blocked**\n- Message: `Harness is not ready for execution.`\n")
        _record_text(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "harness_status": harness_status,
        }, indent=2))
        state["system_software_cosim_execution_report"] = report
        state["system_software_cosim_execution_status"] = "blocked"
        state["status"] = "⚠️ System software co-sim execution blocked"
        return state

    scenarios = harness.get("scenarios") or []
    resolved_commands = harness.get("resolved_commands") or []
    commands_by_scenario: Dict[str, List[Dict[str, Any]]] = {}
    for item in resolved_commands:
        sid = str(item.get("scenario_id") or "").strip()
        if sid:
            commands_by_scenario.setdefault(sid, []).append(item)
    if "__global__" in commands_by_scenario:
        for scenario in scenarios:
            sid = str(scenario.get("scenario_id") or "").strip()
            if sid:
                commands_by_scenario.setdefault(sid, []).extend(commands_by_scenario["__global__"])

    scenario_results: List[Dict[str, Any]] = []
    for scenario in scenarios:
        scenario_id = str(scenario.get("scenario_id") or "").strip()
        timeout_sec = int(scenario.get("timeout_sec") or 600)
        command_results: List[Dict[str, Any]] = []
        commands = commands_by_scenario.get(scenario_id) or []
        cwd = _resolve_cwd(state, scenario)

        if not commands:
            scenario_results.append({
                "scenario_id": scenario_id,
                "scenario_type": scenario.get("scenario_type") or scenario.get("type") or "",
                "execution_status": "blocked",
                "message": "No commands resolved for scenario.",
                "returncode": -1,
                "command_results": [],
                "expected_behavior": _scenario_expectations(scenario),
                "observed_behavior": {},
                "artifacts": {},
            })
            continue

        execution_status = "pass"
        final_returncode = 0
        for command in commands:
            cmd = [str(x) for x in (command.get("command") or []) if str(x).strip()]
            result = _run_cmd(cmd, cwd=cwd, timeout_sec=timeout_sec)
            command_result = {
                "command_id": command.get("command_id") or "",
                "source": command.get("source") or "",
                "command": cmd,
                "cwd": cwd,
                "returncode": result["returncode"],
                "stdout_tail": result["stdout_tail"],
                "stderr_tail": result["stderr_tail"],
            }
            command_results.append(command_result)
            final_returncode = result["returncode"]
            if result["returncode"] != 0:
                execution_status = "fail"
                break

        stdout_text = "\n".join(
            [str(cr.get("stdout_tail") or "") for cr in command_results if isinstance(cr, dict)]
        ).strip()

        observed_behavior = _extract_observations(
            state=state,
            scenario=scenario,
            stdout_text=stdout_text,
        )
                
        scenario_results.append({
            "scenario_id": scenario_id,
            "scenario_type": scenario.get("scenario_type") or scenario.get("type") or "",
            "execution_status": execution_status,
            "returncode": final_returncode,
            "command_results": command_results,
            "expected_behavior": _scenario_expectations(scenario),
            "observed_behavior": observed_behavior,
            "artifacts": {
                "waveform": scenario.get("waveform"),
                "software_log": scenario.get("software_log"),
                "firmware_log": scenario.get("firmware_log"),
                "rtl_log": scenario.get("rtl_log"),
            },
        })

    pass_count = sum(1 for x in scenario_results if x.get("execution_status") == "pass")
    fail_count = sum(1 for x in scenario_results if x.get("execution_status") == "fail")
    blocked_count = sum(1 for x in scenario_results if x.get("execution_status") == "blocked")
    execution_status = "pass" if fail_count == 0 and blocked_count == 0 else ("partial_pass" if pass_count > 0 else "fail")

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "execution_status": execution_status,
        "scenario_count": len(scenario_results),
        "scenario_pass_count": pass_count,
        "scenario_fail_count": fail_count,
        "scenario_blocked_count": blocked_count,
        "scenario_results": scenario_results,
    }

    summary_lines = [
        "# System Software CoSim Execution Summary",
        "",
        f"- Execution status: **{execution_status}**",
        f"- Scenario count: `{len(scenario_results)}`",
        f"- Passed: `{pass_count}`",
        f"- Failed: `{fail_count}`",
        f"- Blocked: `{blocked_count}`",
        "",
        "## Scenario results",
    ]
    if scenario_results:
        for item in scenario_results:
            summary_lines.append(
                f"- `{item['scenario_id']}` → status=`{item['execution_status']}` returncode=`{item['returncode']}`"
            )
    else:
        summary_lines.append("- none")

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "harness_status": harness_status,
        "scenario_count": len(scenario_results),
        "execution_status": execution_status,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_cosim_execution_report"] = report
    state["system_software_cosim_execution_status"] = execution_status
    state["status"] = "✅ System software co-sim execution complete" if execution_status in {"pass", "partial_pass"} else "⚠️ System software co-sim execution failed"
    return state
