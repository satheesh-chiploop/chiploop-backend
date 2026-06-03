import json
import sys

from deployment_modes import deployment_summary
from license_policy import license_summary
from model_gateway import model_profile_summary
from tooling.runner import run_tool_diagnostics


def main() -> int:
    payload = {
        "deployment": deployment_summary(),
        "license": license_summary(),
        "model_profile": model_profile_summary(),
        "tool_diagnostics": run_tool_diagnostics(),
    }
    print(json.dumps(payload, indent=2, default=str))
    required = {"python", "make", "git"}
    results = payload["tool_diagnostics"]["results"]
    missing = sorted(name for name in required if not (results.get(name) or {}).get("available"))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
