import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


DEFAULT_DEPLOYMENT_MODE = "hosted_saas"


@dataclass(frozen=True)
class DeploymentMode:
    mode: str
    label: str
    frontend_owner: str
    backend_owner: str
    data_owner: str
    tool_owner: str
    model_owner: str
    description: str
    capabilities: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEPLOYMENT_MODES: Dict[str, DeploymentMode] = {
    "hosted_saas": DeploymentMode(
        mode="hosted_saas",
        label="ChipLoop Hosted SaaS",
        frontend_owner="chiploop",
        backend_owner="chiploop",
        data_owner="chiploop",
        tool_owner="chiploop",
        model_owner="chiploop",
        description="Default managed ChipLoop service: Vercel frontend, ChipLoop backend, ChipLoop Supabase, ChipLoop tools, and managed model keys.",
        capabilities=["managed_frontend", "managed_backend", "managed_data", "managed_tools", "managed_models"],
    ),
    "hybrid_runner": DeploymentMode(
        mode="hybrid_runner",
        label="ChipLoop Hybrid Runner",
        frontend_owner="chiploop",
        backend_owner="customer",
        data_owner="chiploop",
        tool_owner="customer",
        model_owner="chiploop_or_customer",
        description="ChipLoop manages frontend/data while the customer runs a private backend or runner for local tools and sensitive files.",
        capabilities=["managed_frontend", "managed_data", "private_backend", "private_tools", "runner_job_contract"],
    ),
    "hybrid_private_data": DeploymentMode(
        mode="hybrid_private_data",
        label="ChipLoop Hybrid Private Data",
        frontend_owner="chiploop",
        backend_owner="customer",
        data_owner="customer",
        tool_owner="customer",
        model_owner="customer",
        description="ChipLoop owns the frontend experience while customer backend, database, storage, tools, and model credentials stay private.",
        capabilities=["managed_frontend", "private_backend", "private_data", "private_tools", "customer_models"],
    ),
    "private": DeploymentMode(
        mode="private",
        label="ChipLoop Private",
        frontend_owner="customer",
        backend_owner="customer",
        data_owner="customer",
        tool_owner="customer",
        model_owner="customer",
        description="Fully self-hosted package where the customer manages frontend, backend, database, storage, tools, licenses, and model/API keys.",
        capabilities=["private_frontend", "private_backend", "private_data", "private_tools", "customer_models", "enterprise_license"],
    ),
    "customer_cloud": DeploymentMode(
        mode="customer_cloud",
        label="ChipLoop Customer Cloud",
        frontend_owner="customer_or_chiploop",
        backend_owner="customer",
        data_owner="customer",
        tool_owner="customer",
        model_owner="customer",
        description="Deployment into customer Azure/AWS/GCP with cloud IAM, customer storage, and model providers such as Azure OpenAI or AWS Bedrock.",
        capabilities=["customer_cloud", "private_data", "customer_models", "cloud_iam", "tool_adapters"],
    ),
}


def active_deployment_mode() -> DeploymentMode:
    mode = (
        os.getenv("CHIPLOOP_DEPLOYMENT_MODE")
        or os.getenv("CHIPLOOP_MODE")
        or DEFAULT_DEPLOYMENT_MODE
    ).strip().lower()
    return DEPLOYMENT_MODES.get(mode, DEPLOYMENT_MODES[DEFAULT_DEPLOYMENT_MODE])


def deployment_summary() -> Dict[str, Any]:
    active = active_deployment_mode()
    return {
        "active": active.to_dict(),
        "available_modes": [mode.to_dict() for mode in DEPLOYMENT_MODES.values()],
        "env": {
            "CHIPLOOP_DEPLOYMENT_MODE": os.getenv("CHIPLOOP_DEPLOYMENT_MODE", ""),
            "CHIPLOOP_TOOL_PROFILE_PATH": os.getenv("CHIPLOOP_TOOL_PROFILE_PATH", ""),
            "CHIPLOOP_MODEL_PROFILE_PATH": os.getenv("CHIPLOOP_MODEL_PROFILE_PATH", ""),
        },
    }
