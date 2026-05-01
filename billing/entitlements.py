from typing import Any, Dict

from .models import Entitlements, Plan


PLAN_DEFINITIONS: Dict[str, Plan] = {
    "trial": Plan(
        id="trial",
        name="Trial",
        display_name="7-day Trial",
        price_monthly_usd=0,
        monthly_credits=100,
        entitlements=Entitlements(
            plan_id="trial",
            plan_name="Trial",
            monthly_credits=100,
            max_api_keys=0,
            max_private_agents=1,
            sdk_cli_enabled=False,
            agent_planner_enabled=True,
            agent_factory_dry_run_enabled=True,
            private_agent_save_enabled=True,
            dag_optimization_enabled=False,
            marketplace_submit_enabled=False,
        ),
        metadata={"requires_credit_card": True, "trial_days": 7, "converts_to": "starter"},
    ),
    "starter": Plan(
        id="starter",
        name="Starter",
        display_name="Starter",
        price_monthly_usd=19.99,
        monthly_credits=2000,
        entitlements=Entitlements(
            plan_id="starter",
            plan_name="Starter",
            monthly_credits=2000,
            max_api_keys=0,
            max_private_agents=5,
            sdk_cli_enabled=False,
            agent_planner_enabled=True,
            agent_factory_dry_run_enabled=True,
            private_agent_save_enabled=True,
            dag_optimization_enabled=True,
            marketplace_submit_enabled=False,
        ),
        discount_percent=25,
        discount_months=3,
        metadata={"stripe_price_key": "starter", "discount_percent": 25, "discount_months": 3},
    ),
    "pro": Plan(
        id="pro",
        name="Pro",
        display_name="Pro",
        price_monthly_usd=39.99,
        monthly_credits=5000,
        entitlements=Entitlements(
            plan_id="pro",
            plan_name="Pro",
            monthly_credits=5000,
            max_api_keys=3,
            max_private_agents=25,
            sdk_cli_enabled=True,
            agent_planner_enabled=True,
            agent_factory_dry_run_enabled=True,
            private_agent_save_enabled=True,
            dag_optimization_enabled=True,
            marketplace_submit_enabled=True,
        ),
        discount_percent=25,
        discount_months=3,
        metadata={"stripe_price_key": "pro", "discount_percent": 25, "discount_months": 3, "most_popular": True},
    ),
    "pro_max": Plan(
        id="pro_max",
        name="Pro Max",
        display_name="Pro Max",
        price_monthly_usd=59.99,
        monthly_credits=12000,
        entitlements=Entitlements(
            plan_id="pro_max",
            plan_name="Pro Max",
            monthly_credits=12000,
            max_api_keys=10,
            max_private_agents=100,
            sdk_cli_enabled=True,
            agent_planner_enabled=True,
            agent_factory_dry_run_enabled=True,
            private_agent_save_enabled=True,
            dag_optimization_enabled=True,
            marketplace_submit_enabled=True,
            higher_workflow_limits=True,
        ),
        discount_percent=25,
        discount_months=3,
        metadata={"stripe_price_key": "pro_max", "discount_percent": 25, "discount_months": 3},
    ),
    "enterprise": Plan(
        id="enterprise",
        name="Enterprise",
        display_name="Enterprise",
        price_monthly_usd=None,
        monthly_credits=None,
        entitlements=Entitlements(
            plan_id="enterprise",
            plan_name="Enterprise",
            monthly_credits=None,
            max_api_keys=100,
            max_private_agents=1000,
            sdk_cli_enabled=True,
            agent_planner_enabled=True,
            agent_factory_dry_run_enabled=True,
            private_agent_save_enabled=True,
            dag_optimization_enabled=True,
            marketplace_submit_enabled=True,
            agent_factory_write_enabled=False,
            higher_workflow_limits=True,
            custom_limits=True,
        ),
        metadata={"price_label": "custom", "contact_sales": True},
    ),
}

# Backward-compatible alias for older rows/tests. Product copy should use Trial.
PLAN_DEFINITIONS["free"] = PLAN_DEFINITIONS["trial"]


ACTION_CREDIT_COSTS: Dict[str, int] = {
    "app_workflow_run": 50,
    "sdk_workflow_run": 50,
    "studio_agent_planner": 5,
    "studio_agent_factory_dry_run": 25,
    "private_agent_save": 10,
    "dag_parallelism_analyze": 10,
    "marketplace_submit": 5,
    "sdk_agents_list": 1,
    "sdk_workflows_list": 1,
    "sdk_workflow_status": 1,
    "sdk_workflow_run": 50,
    "sdk_artifacts_list": 1,
    "sdk_artifact_download": 1,
    "sdk_usage": 0,
    "sdk_plan": 0,
    "sdk_studio_plan_agent": 5,
    "sdk_studio_generate_agent": 25,
}


SDK_EVENT_TO_FEATURE: Dict[str, str] = {
    "sdk_agents_list": "sdk_cli_enabled",
    "sdk_workflows_list": "sdk_cli_enabled",
    "sdk_workflow_status": "sdk_cli_enabled",
    "sdk_workflow_run": "sdk_cli_enabled",
    "sdk_artifacts_list": "sdk_cli_enabled",
    "sdk_artifact_download": "sdk_cli_enabled",
    "sdk_usage": "sdk_cli_enabled",
    "sdk_plan": "sdk_cli_enabled",
    "sdk_studio_plan_agent": "agent_planner_enabled",
    "sdk_studio_generate_agent": "agent_factory_dry_run_enabled",
}


def plan_payload(plan: Plan) -> Dict[str, Any]:
    payload = plan.to_dict()
    payload["price"] = "custom" if plan.price_monthly_usd is None else plan.price_monthly_usd
    payload["base_price"] = payload["price"]
    payload["discounted_price"] = (
        None
        if plan.price_monthly_usd is None or not plan.discount_percent
        else round(plan.price_monthly_usd * (1 - plan.discount_percent / 100), 2)
    )
    return payload
