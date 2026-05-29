import datetime
import json
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Product Package Agent"
OUTPUT_SUBDIR = "system/product"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, content: str) -> Optional[str]:
    return save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, filename, content)


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    manifest = state.get("system_product_dashboard_manifest") if isinstance(state.get("system_product_dashboard_manifest"), dict) else {}
    package = {
        "type": "system_product_package",
        "generated_at": _now(),
        "entrypoint": manifest.get("entrypoint"),
        "download_hint": "Download ZIP and open system/product/app/index.html.",
        "runtime": "simulated_device",
        "future_hardware_transports": ["uart", "usb_serial", "jtag_openocd", "ethernet", "fpga_mmio_bridge"],
        "artifacts": {
            "collateral_contract": state.get("system_product_collateral_contract_path"),
            "capability_model": state.get("system_product_capability_model_path"),
            "dashboard": manifest.get("entrypoint"),
        },
    }
    readme = "\n".join([
        "# Generated Product App",
        "",
        "Open `system/product/app/index.html` from the downloaded ZIP.",
        "",
        "This is a simulator-backed app generated from the RTL, firmware, software, and validation workflow lineage.",
        "The simulator adapter can later be replaced with a board or silicon transport.",
        "",
    ])
    _record(workflow_id, "system_product_package.json", json.dumps(package, indent=2))
    _record(workflow_id, "README_PRODUCT_APP.md", readme)
    state["system_product_package"] = package
    state["status"] = "System product package generated"
    return state
