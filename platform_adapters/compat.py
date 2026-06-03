from typing import Any

from .client import get_platform_client


Client = Any


def create_client(url: str = "", key: str = "") -> Any:
    """Compatibility replacement for modules that previously created Supabase clients."""
    return get_platform_client()
