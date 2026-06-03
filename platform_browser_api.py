import os
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from browser_auth import BrowserUser, require_browser_user
from platform_adapters import get_platform_client


router = APIRouter(prefix="/platform", tags=["platform"])

READ_TABLES = {
    "workflows",
    "runs",
    "artifacts",
    "design_intent_drafts",
    "validation_benches",
    "validation_bench_connections",
}
WRITE_TABLES = {"design_intent_drafts"}
FILTER_METHODS = {"eq", "neq", "gt", "gte", "lt", "lte", "in_", "is_", "or_"}


class PlatformFilter(BaseModel):
    method: str
    column: Optional[str] = None
    value: Any = None


class PlatformOrder(BaseModel):
    column: str
    ascending: bool = True


class PlatformQueryRequest(BaseModel):
    table: str
    operation: Literal["select", "insert", "update", "upsert", "delete"] = "select"
    columns: str = "*"
    payload: Any = None
    filters: List[PlatformFilter] = Field(default_factory=list)
    orders: List[PlatformOrder] = Field(default_factory=list)
    limit: Optional[int] = None
    single: bool = False
    maybe_single: bool = False
    count: Optional[str] = None
    on_conflict: Optional[str] = None


def _execute(query: Any) -> Any:
    return query.execute()


def _data(response: Any) -> Any:
    return getattr(response, "data", response)


def _owned_workflow_ids(user_id: str) -> List[str]:
    client = get_platform_client()
    response = _execute(client.table("workflows").select("id").eq("user_id", user_id))
    return [str(row.get("id")) for row in (_data(response) or []) if row.get("id")]


def _owned_bench_ids(user_id: str) -> List[str]:
    client = get_platform_client()
    response = _execute(client.table("validation_benches").select("id,metadata"))
    ids: List[str] = []
    for row in (_data(response) or []):
        metadata = row.get("metadata") or {}
        owner = metadata.get("owner_user_id") or metadata.get("created_by")
        if owner == user_id and row.get("id"):
            ids.append(str(row["id"]))
    return ids


def _apply_scope(query: Any, table: str, operation: str, user: BrowserUser) -> Any:
    if table == "workflows":
        if operation == "select":
            return query.or_(f"user_id.eq.{user.user_id},is_prebuilt.eq.true")
        return query.eq("user_id", user.user_id)
    if table == "design_intent_drafts":
        return query.eq("user_id", user.user_id)
    if table in {"runs", "artifacts"}:
        workflow_ids = _owned_workflow_ids(user.user_id)
        return query.in_("workflow_id", workflow_ids)
    if table == "validation_bench_connections":
        # Connections are only reachable through benches owned by the browser user.
        return query.in_("bench_id", _owned_bench_ids(user.user_id))
    if table == "validation_benches":
        return query.in_("id", _owned_bench_ids(user.user_id))
    return query


def _apply_filters(query: Any, filters: List[PlatformFilter]) -> Any:
    for item in filters:
        if item.method not in FILTER_METHODS:
            raise HTTPException(status_code=400, detail=f"unsupported_filter:{item.method}")
        if item.method == "or_":
            query = query.or_(str(item.value or ""))
            continue
        if not item.column:
            raise HTTPException(status_code=400, detail=f"missing_filter_column:{item.method}")
        query = getattr(query, item.method)(item.column, item.value)
    return query


@router.get("/session")
def platform_session(user: BrowserUser = Depends(require_browser_user)) -> Dict[str, Any]:
    return {
        "user": {
            "id": user.user_id,
            "email": str(user.claims.get("email") or ""),
            "user_metadata": user.claims.get("user_metadata") or {},
        }
    }


@router.post("/query")
def platform_query(payload: PlatformQueryRequest, user: BrowserUser = Depends(require_browser_user)) -> Dict[str, Any]:
    if payload.table not in READ_TABLES:
        raise HTTPException(status_code=403, detail="platform_table_not_allowed")
    if payload.operation != "select" and payload.table not in WRITE_TABLES:
        raise HTTPException(status_code=403, detail="platform_table_write_not_allowed")

    client = get_platform_client()
    query = client.table(payload.table)
    write_payload = payload.payload
    if payload.table == "design_intent_drafts" and payload.operation != "select":
        if isinstance(write_payload, list):
            write_payload = [{**(row or {}), "user_id": user.user_id} for row in write_payload]
        else:
            write_payload = {**(write_payload or {}), "user_id": user.user_id}
    if payload.operation == "select":
        query = query.select(payload.columns, count=payload.count)
    elif payload.operation == "insert":
        query = query.insert(write_payload)
    elif payload.operation == "update":
        query = query.update(write_payload or {})
    elif payload.operation == "upsert":
        query = query.upsert(write_payload, on_conflict=payload.on_conflict)
    else:
        query = query.delete()

    query = _apply_scope(query, payload.table, payload.operation, user)
    query = _apply_filters(query, payload.filters)
    for order in payload.orders:
        query = query.order(order.column, desc=not order.ascending)
    if payload.limit is not None:
        query = query.limit(max(0, min(int(payload.limit), 1000)))
    if payload.single:
        query = query.single()
    elif payload.maybe_single:
        query = query.maybe_single()

    try:
        response = _execute(query)
        return {"data": _data(response), "count": getattr(response, "count", None), "error": None}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"platform_query_failed:{type(exc).__name__}:{exc}") from exc


@router.get("/storage/{bucket}/{path:path}")
def platform_storage_download(
    bucket: str,
    path: str,
    user: BrowserUser = Depends(require_browser_user),
) -> Response:
    allowed_bucket = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")
    if bucket != allowed_bucket:
        raise HTTPException(status_code=403, detail="platform_storage_bucket_not_allowed")
    owned_prefix = f"backend/workflows/"
    if not path.startswith(owned_prefix):
        raise HTTPException(status_code=403, detail="platform_storage_path_not_allowed")
    parts = path.split("/")
    if len(parts) < 3 or parts[2] not in set(_owned_workflow_ids(user.user_id)):
        raise HTTPException(status_code=403, detail="platform_storage_path_not_owned")
    try:
        content = get_platform_client().storage.from_(bucket).download(path)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"platform_storage_not_found:{exc}") from exc
    return Response(content=content, media_type="application/octet-stream")
