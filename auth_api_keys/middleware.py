from typing import Callable

from fastapi import HTTPException, Request

from .service import get_api_key_service


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def require_sdk_api_key(event_type: str) -> Callable:
    async def dependency(request: Request):
        service = get_api_key_service(getattr(request.app.state, "supabase", None))
        validation = service.validate_key(_bearer_token(request))
        if not validation.ok or validation.record is None:
            raise HTTPException(status_code=401, detail=validation.error or "missing_api_key")

        entitlement = service.get_entitlement(validation.record.user_id)
        if not entitlement.sdk_cli_enabled:
            raise HTTPException(status_code=403, detail="sdk_cli_not_enabled")

        request.state.user_id = validation.record.user_id
        request.state.api_key_id = validation.record.id
        request.state.entitlement = entitlement

        service.record_usage(
            user_id=validation.record.user_id,
            api_key_id=validation.record.id,
            endpoint=request.url.path,
            event_type=event_type,
            workflow_id=request.path_params.get("workflow_id"),
        )
        return validation.record

    return dependency
