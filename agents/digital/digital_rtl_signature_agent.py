import os
import re
import json
from datetime import datetime
from typing import Dict, Any

from utils.artifact_utils import save_text_artifact_and_record


def _now():
    return datetime.now().isoformat()


def _record(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str):
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def extract_modules_and_ports(workflow_dir: str) -> Dict[str, Any]:
    module_pattern = re.compile(r"module\s+(\w+)")
    port_pattern = re.compile(r"(input|output|inout)\s+(?:logic|wire|reg)?\s*(\[[^\]]+\])?\s*(\w+)")

    signatures = {}

    for root, _, files in os.walk(workflow_dir):
        for f in files:
            if f.endswith((".sv", ".v")):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()

                    module_match = module_pattern.search(content)
                    if not module_match:
                        continue

                    module_name = module_match.group(1)
                    signatures[module_name] = {"ports": {}}

                    for pm in port_pattern.finditer(content):
                        direction = pm.group(1)
                        width = pm.group(2) or "1"
                        name = pm.group(3)

                        signatures[module_name]["ports"][name] = {
                            "dir": direction,
                            "width": width,
                        }

                except Exception:
                    continue

    return signatures


def run_agent(state: dict) -> dict:
    agent_name = "Digital RTL Signature Agent"
    workflow_id = state.get("workflow_id")
    workflow_dir = state.get("workflow_dir")

    signatures = extract_modules_and_ports(workflow_dir)

    json_content = json.dumps(signatures, indent=2)

    _record(
        workflow_id,
        agent_name,
        "digital/integrate",
        "rtl_signatures.json",
        json_content,
    )

    state["rtl_signatures"] = signatures
    state["status"] = "✅ RTL signatures extracted"
    return state
