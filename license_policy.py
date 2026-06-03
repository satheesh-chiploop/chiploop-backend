import os
from typing import Any, Dict

from deployment_modes import active_deployment_mode


DEFAULT_LICENSE_MODES = {
    "hosted_saas": "hosted_subscription",
    "hybrid_runner": "hybrid_runner",
    "hybrid_private_data": "hybrid_runner",
    "private": "private_enterprise",
    "customer_cloud": "customer_cloud",
}


def license_summary() -> Dict[str, Any]:
    deployment_mode = active_deployment_mode().mode
    mode = os.getenv("CHIPLOOP_LICENSE_MODE", DEFAULT_LICENSE_MODES.get(deployment_mode, "hosted_subscription"))
    key = os.getenv("CHIPLOOP_LICENSE_KEY", "")
    managed = mode == "hosted_subscription"
    return {
        "mode": mode,
        "managed_by_chiploop": managed,
        "license_key_configured": managed or bool(key),
        "third_party_tool_licenses": "customer_managed" if deployment_mode != "hosted_saas" else "chiploop_or_customer_managed",
    }
