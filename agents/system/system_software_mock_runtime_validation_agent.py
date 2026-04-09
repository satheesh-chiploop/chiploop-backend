import datetime
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Mock Runtime Validation Agent"
OUTPUT_SUBDIR = "system/software_validation/mock"

REPORT_JSON = "mock_runtime_validation_report.json"
SUMMARY_MD = "mock_runtime_validation_summary.md"
DEBUG_JSON = "mock_runtime_validation_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if isinstance(text, str) else ""


def _tool_env() -> Dict[str, str]:
    env = os.environ.copy()
    preferred_bin = "/root/.cargo/bin"
    path_parts = env.get("PATH", "").split(":") if env.get("PATH") else []
    if preferred_bin not in path_parts:
        env["PATH"] = preferred_bin + (":" + env["PATH"] if env.get("PATH") else "")
    if not env.get("CARGO_HOME"):
        env["CARGO_HOME"] = "/root/.cargo"
    if not env.get("RUSTUP_HOME"):
        env["RUSTUP_HOME"] = "/root/.rustup"
    return env


def _find_cargo() -> str:
    env = _tool_env()
    for candidate in [
        "/root/.cargo/bin/cargo",
        shutil.which("cargo", path=env.get("PATH")),
        shutil.which("cargo"),
    ]:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _run_cmd(cmd: List[str], cwd: str, timeout_sec: int = 600) -> Dict[str, Any]:
    env = _tool_env()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        return {
            "returncode": result.returncode,
            "stdout": _tail(result.stdout),
            "stderr": _tail(result.stderr),
        }
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        }


def _detect_tool_versions(cargo_bin: str) -> Dict[str, str]:
    cargo_version = ""
    rustc_version = ""
    if cargo_bin:
        cargo_result = _run_cmd([cargo_bin, "--version"], cwd="/root")
        cargo_version = (cargo_result.get("stdout") or cargo_result.get("stderr") or "").strip()
    rustc_bin = shutil.which("rustc", path=_tool_env().get("PATH"))
    if rustc_bin:
        rustc_result = _run_cmd([rustc_bin, "--version"], cwd="/root")
        rustc_version = (rustc_result.get("stdout") or rustc_result.get("stderr") or "").strip()
    return {
        "cargo_bin": cargo_bin,
        "cargo_version": cargo_version,
        "rustc_version": rustc_version,
    }


def _workspace_supports_feature(build_root: str, feature_name: str) -> bool:
    cargo_toml = os.path.join(build_root, "Cargo.toml")
    if not os.path.isfile(cargo_toml):
        return False
    try:
        with open(cargo_toml, "r", encoding="utf-8") as f:
            text = f.read()
        return feature_name in text
    except Exception:
        return False

def _candidate_mock_targets(mock_manifest: Dict[str, Any], cargo_bin: str, build_root: str) -> List[List[str]]:
    candidates: List[List[str]] = []

    explicit = mock_manifest.get("commands") or []
    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, list) and item:
                normalized = [str(x) for x in item]
                if normalized and normalized[0] == "cargo" and cargo_bin:
                    normalized[0] = cargo_bin
                candidates.append(normalized)
            elif _is_nonempty_str(item):
                parts = str(item).strip().split()
                if parts:
                    if parts[0] == "cargo":
                        parts[0] = cargo_bin
                    candidates.append(parts)

    if _workspace_supports_feature(build_root, "mock"):
        candidates.append([cargo_bin, "test", "--workspace", "--features", "mock"])

    candidates.extend([
        [cargo_bin, "test", "--workspace", "--all-targets"],
        [cargo_bin, "test", "--workspace"],
        [cargo_bin, "test"],
    ])

    seen = set()
    deduped: List[List[str]] = []
    for cmd in candidates:
        key = tuple(cmd)
        if key not in seen:
            seen.add(key)
            deduped.append(cmd)
    return deduped


def _resolve_dir_from_manifest(state: Dict[str, Any], asset_key: str) -> str:
    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered = validation_manifest.get("discovered_assets") or {}
    info = discovered.get(asset_key) or {}
    resolved_manifest_path = str(info.get("resolved_path") or "").strip()
    if resolved_manifest_path:
        candidate = os.path.dirname(resolved_manifest_path)
        if os.path.isdir(candidate):
            return candidate
    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    print(f"\n🧪 Running {AGENT_NAME}")

    validation_manifest = state.get("system_software_validation_manifest") or {}
    mock_manifest = state.get("system_software_mock_manifest") or {}

    build_root = state.get("system_software_build_root") or _resolve_dir_from_manifest(state, "build_manifest")
    mock_root = state.get("system_software_mock_root") or _resolve_dir_from_manifest(state, "mock_manifest")

    if not build_root:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "validation_scope": "software_only",
            "mock_runtime_status": "not_present",
            "message": "No Cargo workspace/build root could be resolved for mock runtime validation.",
        }
        _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record_text(workflow_id, SUMMARY_MD, "# Mock Runtime Validation\n\n- Status: **not_present**\n- Message: `No Cargo workspace/build root could be resolved for mock runtime validation.`\n")
        _record_text(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "reason": "mock_runtime_validation_build_root_missing",
            "resolved_build_manifest_path": (
                ((validation_manifest.get("discovered_assets") or {})
                 .get("build_manifest") or {})
                .get("resolved_path", "")
            ),
            "resolved_mock_manifest_path": (
                ((validation_manifest.get("discovered_assets") or {})
                 .get("mock_manifest") or {})
                .get("resolved_path", "")
            ),
        }, indent=2))
        state["system_software_mock_runtime_validation"] = report
        state["mock_runtime_status"] = "not_present"
        state["status"] = "⚠️ mock runtime build root not present"
        return state

    manifest_missing = not bool(mock_manifest)
    mock_root_missing = not bool(mock_root)

    cargo_bin = _find_cargo()
    if not cargo_bin:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "validation_scope": "software_only",
            "co_sim_enabled": False,
            "build_root": build_root,
            "mock_root": mock_root,
            "mock_runtime_status": "environment_missing",
            "message": "cargo not found on PATH",
        }
        _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record_text(
            workflow_id,
            SUMMARY_MD,
            "# Mock Runtime Validation\n\n"
            "- Status: **environment_missing**\n"
            f"- Build root: `{build_root}`\n"
            "- Message: `cargo not found on PATH`\n",
        )
        _record_text(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "build_root": build_root,
            "mock_root": mock_root,
            "cargo_bin": cargo_bin,
            "PATH": os.environ.get("PATH", ""),
        }, indent=2))
        state["system_software_mock_runtime_validation"] = report
        state["mock_runtime_status"] = "environment_missing"
        state["status"] = "⚠️ mock runtime environment missing"
        return state


    tool_versions = _detect_tool_versions(cargo_bin)

    attempts = []
    selected = None
    for cmd in _candidate_mock_targets(mock_manifest, cargo_bin, build_root):
        result = _run_cmd(cmd, cwd=build_root)
        attempts.append({
            "command": cmd,
            "returncode": result["returncode"],
            "stdout_tail": result["stdout"],
            "stderr_tail": result["stderr"],
        })
        if result["returncode"] == 0:
            selected = attempts[-1]
            break

    

    final_attempt = selected or (attempts[-1] if attempts else {
        "command": [],
        "returncode": -1,
        "stdout_tail": "",
        "stderr_tail": "No command candidates were generated.",
    })

    mock_runtime_status = "pass" if final_attempt["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "validation_scope": "software_only",
        "co_sim_enabled": False,
        "build_root": build_root,
        "mock_root": mock_root,
        "mock_manifest_present": not manifest_missing,
        "mock_root_present": not mock_root_missing,
        "cargo_bin": cargo_bin,
        "cargo_version": tool_versions.get("cargo_version", ""),
        "rustc_version": tool_versions.get("rustc_version", ""),
        "mock_runtime_status": mock_runtime_status,
        "selected_command": final_attempt["command"],
        "returncode": final_attempt["returncode"],
        "stdout_tail": final_attempt["stdout_tail"],
        "stderr_tail": final_attempt["stderr_tail"],
        "attempt_count": len(attempts),
    }

    summary = (
        "# Mock Runtime Validation\n\n"
        f"- Status: **{mock_runtime_status}**\n"
        f"- Build root: `{build_root}`\n"
        f"- Mock manifest present: `{not manifest_missing}`\n"
        f"- Mock root present: `{not mock_root_missing}`\n"
        f"- Cargo: `{cargo_bin}`\n"
        f"- Command: `{ ' '.join(final_attempt['command']) }`\n"
        f"- Return code: `{final_attempt['returncode']}`\n"
    )

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "validation_manifest_present": bool(validation_manifest),
        "mock_manifest_present": bool(mock_manifest),
        "cargo_bin": cargo_bin,
        "cargo_version": tool_versions.get("cargo_version", ""),
        "rustc_version": tool_versions.get("rustc_version", ""),
        "PATH": _tool_env().get("PATH", ""),
        "attempts": attempts,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, summary)
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_mock_runtime_validation"] = report
    state["mock_runtime_status"] = mock_runtime_status
    state["status"] = "✅ mock runtime validation complete" if mock_runtime_status == "pass" else "⚠️ mock runtime validation failed"
    return state
