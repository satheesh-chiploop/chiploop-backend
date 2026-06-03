from datetime import datetime
from typing import Any, Dict

from deployment_modes import deployment_summary
from license_policy import license_summary
from model_gateway import model_profile_summary
from platform_services import platform_services_summary
from tooling.profiles import profile_diagnostics


def build_readiness_payload(platform_client: Any) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    try:
        platform_client.table("workflows").select("id").limit(1).execute()
        checks["data_store"] = {"ok": True}
    except Exception as exc:
        checks["data_store"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    tools = profile_diagnostics()
    configured_tools = tools.get("tools") if isinstance(tools, dict) else {}
    available_tools = [
        name for name, info in (configured_tools or {}).items()
        if isinstance(info, dict) and bool(info.get("available"))
    ]
    checks["tool_profile"] = {
        "ok": bool(configured_tools),
        "profile_id": tools.get("profile_id") if isinstance(tools, dict) else None,
        "configured_tool_count": len(configured_tools or {}),
        "available_tool_count": len(available_tools),
    }

    model = model_profile_summary()
    checks["model_profile"] = {
        "ok": bool(model.get("provider") or model.get("model_profile_id")),
        "model_profile_id": model.get("model_profile_id"),
        "provider": model.get("provider"),
    }

    license_info = license_summary()
    checks["license"] = {
        "ok": bool(license_info.get("license_key_configured")),
        "mode": license_info.get("mode"),
    }

    ok = all(bool(item.get("ok")) for item in checks.values())
    return {
        "ok": ok,
        "ts": datetime.utcnow().isoformat(),
        "deployment": deployment_summary()["active"],
        "platform_services": platform_services_summary(),
        "checks": checks,
    }
