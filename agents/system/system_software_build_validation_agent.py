import datetime
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Build Validation Agent"
OUTPUT_SUBDIR = "system/software_validation/build"

REPORT_JSON = "build_validation_report.json"
SUMMARY_MD = "build_validation_summary.md"
DEBUG_JSON = "build_validation_debug.json"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id, filename, content):
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
    current_path = env.get("PATH", "")
    path_parts = current_path.split(":") if current_path else []
    if preferred_bin not in path_parts:
        env["PATH"] = preferred_bin + (":" + current_path if current_path else "")
    env.setdefault("CARGO_HOME", "/root/.cargo")
    env.setdefault("RUSTUP_HOME", "/root/.rustup")
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
        result = _run_cmd([cargo_bin, "--version"], cwd="/root")
        cargo_version = (result.get("stdout") or result.get("stderr") or "").strip()

    rustc_bin = shutil.which("rustc", path=_tool_env().get("PATH"))
    if rustc_bin:
        result = _run_cmd([rustc_bin, "--version"], cwd="/root")
        rustc_version = (result.get("stdout") or result.get("stderr") or "").strip()

    return {
        "cargo_bin": cargo_bin,
        "cargo_version": cargo_version,
        "rustc_version": rustc_version,
    }

def _resolve_build_root(state: Dict[str, Any]) -> str:
    candidates = []

    explicit = state.get("system_software_build_root")
    if isinstance(explicit, str) and explicit.strip():
        candidates.append(explicit.strip())

    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered = validation_manifest.get("discovered_assets") or {}

    for asset_key in ["build_manifest", "package_manifest", "sdk_manifest", "workspace_manifest"]:
        info = discovered.get(asset_key) or {}
        resolved_path = str(info.get("resolved_path") or "").strip()
        if resolved_path:
            candidates.append(os.path.dirname(resolved_path))

    workflow_dir = str(state.get("workflow_dir") or "").strip()
    if workflow_dir:
        candidates.extend([
            os.path.join(workflow_dir, "system/software"),
            os.path.join(workflow_dir, "system/software/build"),
            os.path.join(workflow_dir, "system"),
        ])

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            cargo_toml = os.path.join(candidate, "Cargo.toml")
            if os.path.isfile(cargo_toml):
                return candidate

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            for root, _, files in os.walk(candidate):
                if "Cargo.toml" in files:
                    return root

    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    print(f"\n⚙️ Running {AGENT_NAME}")

    build_manifest = state.get("system_software_build_manifest") or {}
    build_root = _resolve_build_root(state)

    if not build_root:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "build_root": "",
            "workspace_members": build_manifest.get("workspace_members") or [],
            "build_status": "not_present",
            "message": "No Cargo workspace/build root could be resolved.",
        }
        debug = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "reason": "build_root_missing",
            "workflow_dir": state.get("workflow_dir") or "",
            "resolved_build_manifest_path": (
                ((state.get("system_software_validation_manifest") or {})
                 .get("discovered_assets") or {})
                .get("build_manifest", {})
                .get("resolved_path", "")
            ),
        }
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record(
            workflow_id,
            SUMMARY_MD,
            "# Build Validation Summary\n\n"
            "- Status: **not_present**\n"
            "- Message: `No Cargo workspace/build root could be resolved.`\n",
        )
        _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))
        state["system_software_build_validation"] = report
        state["build_status"] = "not_present"
        state["status"] = "⚠️ build root not present"
        return state

    cargo_bin = _find_cargo()
    if not cargo_bin:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "build_root": build_root,
            "workspace_members": build_manifest.get("workspace_members") or [],
            "build_status": "environment_missing",
            "message": "cargo not found on PATH",
        }
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record(
            workflow_id,
            SUMMARY_MD,
            "# Build Validation Summary\n\n"
            "- Status: **environment_missing**\n"
            f"- Build root: `{build_root}`\n"
            "- Message: `cargo not found on PATH`\n",
        )
        _record(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "build_root": build_root,
            "cargo_bin": cargo_bin,
            "PATH": os.environ.get("PATH", ""),
        }, indent=2))
        state["system_software_build_validation"] = report
        state["system_software_build_root"] = build_root
        state["build_status"] = "environment_missing"
        state["status"] = "⚠️ build environment missing"
        return state


    tool_versions = _detect_tool_versions(cargo_bin)

    result = _run_cmd([cargo_bin, "build", "--workspace"], build_root)
    build_status = "pass" if result["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "build_root": build_root,
        "workspace_members": build_manifest.get("workspace_members") or [],
        "cargo_bin": cargo_bin,
        "cargo_version": tool_versions.get("cargo_version", ""),
        "rustc_version": tool_versions.get("rustc_version", ""),
        "returncode": result["returncode"],
        "build_status": build_status,
        "stdout_tail": result["stdout"],
        "stderr_tail": result["stderr"],
    }



    summary = (
        "# Build Validation Summary\n\n"
        f"- Status: **{build_status}**\n"
        f"- Build root: `{build_root}`\n"
        f"- Cargo: `{cargo_bin}`\n"
        f"- Cargo version: `{tool_versions.get('cargo_version', '')}`\n"
        f"- Rustc version: `{tool_versions.get('rustc_version', '')}`\n"
        f"- Return code: `{result['returncode']}`\n"
    )

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)


    _record(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "build_root": build_root,
        "cargo_bin": cargo_bin,
        "cargo_version": tool_versions.get("cargo_version", ""),
        "rustc_version": tool_versions.get("rustc_version", ""),
        "PATH": _tool_env().get("PATH", ""),
    }, indent=2))

    state["system_software_build_validation"] = report
    state["system_software_build_root"] = build_root
    state["build_status"] = build_status
    state["status"] = "✅ build validation complete" if build_status == "pass" else "⚠️ build failed"
    return state
