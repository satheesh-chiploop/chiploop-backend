from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Entitlements:
    plan_id: str = "free"
    plan_name: str = "Free"
    monthly_credits: Optional[int] = 100
    max_api_keys: int = 1
    max_private_agents: int = 1
    sdk_cli_enabled: bool = True
    agent_planner_enabled: bool = True
    agent_factory_dry_run_enabled: bool = True
    private_agent_save_enabled: bool = True
    dag_optimization_enabled: bool = False
    marketplace_submit_enabled: bool = False
    agent_factory_write_enabled: bool = False
    higher_workflow_limits: bool = False
    custom_limits: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Entitlements":
        return cls(**{item.name: data[item.name] for item in fields(cls) if item.name in data})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    id: str
    name: str
    display_name: str
    price_monthly_usd: Optional[float]
    monthly_credits: Optional[int]
    entitlements: Entitlements
    active: bool = True
    discount_percent: int = 0
    discount_months: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        plan_id = str(data.get("id") or data.get("plan_id") or "free")
        metadata = data.get("metadata") or {}
        entitlements_data = dict(metadata.get("entitlements") or {})
        entitlements_data.setdefault("plan_id", plan_id)
        entitlements_data.setdefault("plan_name", str(data.get("name") or plan_id.title()))
        entitlements_data.setdefault(
            "monthly_credits",
            data.get("monthly_credits", data.get("monthly_credit_limit")),
        )
        entitlements = Entitlements.from_dict(entitlements_data)
        return cls(
            id=plan_id,
            name=str(data.get("name") or entitlements.plan_name),
            display_name=str(data.get("display_name") or data.get("name") or entitlements.plan_name),
            price_monthly_usd=data.get("price_monthly", data.get("price_monthly_usd")),
            monthly_credits=data.get("monthly_credits", data.get("monthly_credit_limit")),
            entitlements=entitlements,
            active=bool(data.get("active", True)),
            discount_percent=int(data.get("discount_percent") or metadata.get("discount_percent") or 0),
            discount_months=int(data.get("discount_months") or metadata.get("discount_months") or 0),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "price_monthly_usd": self.price_monthly_usd,
            "monthly_credits": self.monthly_credits,
            "entitlements": self.entitlements.to_dict(),
            "active": self.active,
            "price_monthly": self.price_monthly_usd,
            "discount_percent": self.discount_percent,
            "discount_months": self.discount_months,
            "metadata": self.metadata,
        }


@dataclass
class UsageEstimate:
    action_type: str
    credits: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditLedgerEntry:
    user_id: str
    event_type: str
    credits_delta: int
    created_at: str = field(default_factory=utcnow)
    reference_id: Optional[str] = None
    workflow_id: Optional[str] = None
    api_key_id: Optional[str] = None
    balance_after: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        row = asdict(self)
        if self.reference_id:
            row["metadata"] = {**self.metadata, "reference_id": self.reference_id}
        return row


@dataclass
class SubscriptionRecord:
    id: str = ""
    user_id: str = ""
    plan_id: str = "trial"
    status: str = "trialing"
    trial_start_at: Optional[str] = None
    trial_end_at: Optional[str] = None
    trial_status: Optional[str] = "active"
    auto_renew: bool = True
    discount_end_at: Optional[str] = None
    plan_price_effective: Optional[float] = None
    billing_cycle_count: int = 0
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    billing_status: str = "placeholder"
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    stripe_checkout_session_id: Optional[str] = None
    cancel_at_period_end: bool = False
    payment_failure_at: Optional[str] = None
    grace_period_end_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubscriptionRecord":
        return cls(**{item.name: data[item.name] for item in fields(cls) if item.name in data})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
