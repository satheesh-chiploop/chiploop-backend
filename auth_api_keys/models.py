from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class APIKeyRecord:
    id: str
    key_hash: str
    key_prefix: str
    user_id: str
    name: str
    created_at: str
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "APIKeyRecord":
        return cls(
            id=str(data.get("id") or ""),
            key_hash=str(data.get("key_hash") or ""),
            key_prefix=str(data.get("key_prefix") or ""),
            user_id=str(data.get("user_id") or ""),
            name=str(data.get("name") or ""),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            revoked_at=data.get("revoked_at"),
            last_used_at=data.get("last_used_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class APIKeyValidation:
    ok: bool
    record: Optional[APIKeyRecord] = None
    error: str = ""


@dataclass
class UsageEvent:
    user_id: str
    api_key_id: str
    endpoint: str
    event_type: str
    created_at: str
    workflow_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Entitlement:
    sdk_cli_enabled: bool = True
    monthly_credit_limit: int = 1_000_000
    agent_factory_write_enabled: bool = False
