# utils/artifact_utils.py

import os
import json
import logging
from typing import Any, Dict, Optional

from supabase import create_client, Client  # supabase-py v2

logger = logging.getLogger("chiploop")

# --- Supabase client (same pattern as main.py, but local to this module) ---

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "artifact_utils: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
        "— check your environment variables."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Default bucket name (matches how you're writing artifacts now)
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")


def _safe_get_artifacts(workflow_id: str) -> Dict[str, Any]:
    """
    Read the current artifacts JSON from workflows.artifacts.
    Returns a dict (empty if null).
    """
    res = (
        supabase.table("workflows")
        .select("artifacts")
        .eq("id", workflow_id)
        .single()
        .execute()
    )

    data = getattr(res, "data", None) or res  # depending on supabase-py version
    artifacts = (data or {}).get("artifacts")
    if artifacts is None:
        return {}

    if isinstance(artifacts, str):
        try:
            return json.loads(artifacts)
        except Exception:
            logger.warning("artifact_utils: artifacts column is string but not JSON; resetting.")
            return {}

    if isinstance(artifacts, dict):
        return artifacts

    logger.warning("artifact_utils: artifacts column has unexpected type; resetting.")
    return {}

def append_artifact_record(
    workflow_id: str,
    agent_name: str,
    key: str,
    storage_path: str,
) -> None:
    """
    Append/merge a single artifact entry into workflows.artifacts.

    Note: Storage is the source of truth. This DB field is a best-effort index and is
    allowed to compact/overflow to avoid PostgREST payload limits.
    """
    try:
        artifacts = _safe_get_artifacts(workflow_id)

        agent_entry = artifacts.get(agent_name)
        if not isinstance(agent_entry, dict):
            agent_entry = {}

        # Store exactly the string path that Supabase Storage uses
        agent_entry[key] = storage_path
        artifacts[agent_name] = agent_entry
        MAX_ARTIFACTS_JSON_CHARS = 7500
        # Best-effort: keep the DB artifacts index small. Storage is source of truth.
        payload = json.dumps(artifacts, ensure_ascii=False, separators=(",", ":"))
        payload_len = len(payload)
      
        if payload_len > MAX_ARTIFACTS_JSON_CHARS:
            # Fall back to a compact pointer + the current artifact only.
            logger.warning(
                f"artifact_utils: artifacts index too large ({payload_len} chars). "
                f"Compacting for workflow={workflow_id}."
            )
            artifacts = {
                "__mode": "prefix",
                "__prefix": f"backend/workflows/{workflow_id}/",
                agent_name: {key: storage_path},
            }

        try:
            supabase.table("workflows").update({"artifacts": artifacts}).eq("id", workflow_id).execute()
            logger.info(
                f"artifact_utils: Updated artifacts for workflow={workflow_id}, "
                f"agent={agent_name}, key={key}, path={storage_path}"
            )
        except Exception as e:
            # If DB update fails (often due to payload size), don't fail the run —
            # the artifact is already uploaded to Storage.
            logger.error(
                f"artifact_utils: Failed to append artifact record for workflow={workflow_id}, "
                f"agent={agent_name}, key={key}: {e}"
            )
            return

    except Exception as exc:
        logger.exception(
            f"artifact_utils: Failed to append artifact record for workflow={workflow_id}, "
            f"agent={agent_name}, key={key}: {exc}"
        )





def save_text_artifact_and_record(
    workflow_id: str,
    agent_name: str,
    subdir: str,
    filename: str,
    content: str,
    tenant: str = "backend",
) -> Optional[str]:
    """
    Helper to:
    1) Upload a text artifact to Supabase Storage
    2) Record the storage path in workflows.artifacts via append_artifact_record

    Returns the storage path (string) or None on failure.

    Example resulting path:
      backend/workflows/<workflow_id>/rtl/rtl_agent_compile.log
    """
    try:
        if not content:
            logger.warning(
                f"artifact_utils: Empty content for {agent_name}/{filename}; skipping upload"
            )
            return None

        # Path inside the bucket
        storage_path = f"{tenant}/workflows/{workflow_id}/{subdir.rstrip('/')}/{filename}"

        # Upload to the 'artifacts' bucket
        from supabase.lib.client_options import ClientOptions  # (import here to avoid issues)

        # supabase.storage.from_(bucket).upload(path, content[, ...])
        storage = supabase.storage.from_(ARTIFACT_BUCKET)


        try:
          storage.upload(storage_path, content.encode("utf-8"), {"content-type": "text/plain"})
        except Exception as e:
          # overwrite existing file if it already exists (409 Duplicate)
          storage.update(storage_path, content.encode("utf-8"), {"content-type": "text/plain"})
        # Record in workflows.artifacts — key is usually the filename or a logical name
        key = filename
        append_artifact_record(workflow_id, agent_name, key, storage_path)

        logger.info(
            f"artifact_utils: Uploaded and recorded artifact "
            f"workflow={workflow_id}, agent={agent_name}, path={storage_path}"
        )
        return storage_path

    except Exception as exc:
        logger.exception(
            f"artifact_utils: Failed to upload artifact for workflow={workflow_id}, "
            f"agent={agent_name}, file={filename}: {exc}"
        )
        return None
