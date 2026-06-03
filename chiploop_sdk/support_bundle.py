import json
import os
import platform
import sys
from pathlib import Path

from artifact_policy import artifact_policy_summary
from deployment_modes import deployment_summary
from license_policy import license_summary
from model_gateway import model_profile_summary
from platform_services import platform_services_summary
from tooling.runner import run_tool_diagnostics


def main() -> int:
    output = Path(os.getenv("CHIPLOOP_SUPPORT_BUNDLE_PATH", "chiploop_support_bundle.json"))
    payload = {
        "platform": {
            "python": sys.version,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "deployment": deployment_summary(),
        "artifact_policy": artifact_policy_summary(),
        "platform_services": platform_services_summary(),
        "license": license_summary(),
        "model_profile": model_profile_summary(),
        "tool_diagnostics": run_tool_diagnostics(),
    }
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(str(output.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
