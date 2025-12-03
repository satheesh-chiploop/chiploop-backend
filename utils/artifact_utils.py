import os
import uuid
import logging
from typing import Dict, List, Optional

from supabase_client import get_supabase_admin_client

logger = logging.getLogger("chiploop")

supabase_client = get_supabase_admin_client()


# ---------- Core helpers ----------

def _normalize_artifacts_dict(raw) -> Dict[str, List[str]]:
    """
    Ensure artifacts is always a dict[str, list[str]].
    Handles None, empty, or wrong types gracefully.
    """
    if not raw:
        return {}

    if isinstance(raw, dict):
        fixed: Dict[str, List[str]] = {}
        for k, v in raw.items():
            if v is None:
                continue
            if isinstance(v, list):
                fixed[k] = [str(x) for x in v if x]
            else:
                fixed[k] = [str(v)]
        return fixed

    # Anything else – reset
    return {}


# ---------- Public API ----------

def reset_workflow_artifacts(workflow_id: str) -> None:
    """
    Reset artifacts field for a workflow to an empty dict.
    Use this at the start of a run if you want a clean slate.
    """
    try:
        logger.info(f"[ARTIFACTS] Resetting artifacts for workflow_id={workflow_id}")
        supabase_client.table("workflows").update(
            {"artifacts": {}}
        ).eq("id", workflow_id).execute()
    except Exception as e:
        logger.error(f"[ARTIFACTS] Failed to reset artifacts for {workflow_id}: {e}")


def append_artifact_record(
    workflow_id: str,
    key: str,
    path: Optional[str],
) -> None:
    """
    Append a single artifact path under a logical key for the workflow.

    Example:
      append_artifact_record(workflow_id, "rtl_agent_lint_feedback",
                             "anonymous/workflows/<id>/rtl/rtl_agent_lint_feedback.txt")
    """
    if not path:
        logger.warning(f"[ARTIFACTS] Not recording artifact for key={key}: empty path")
        return

    path = str(path)

    try:
        resp = supabase_client.table("workflows") \
            .select("artifacts") \
            .eq("id", workflow_id) \
            .single() \
            .execute()

        data = getattr(resp, "data", resp)
        existing_raw = data.get("artifacts") if data else {}
        artifacts = _normalize_artifacts_dict(existing_raw)

        current_list = artifacts.get(key, [])
        if path not in current_list:
            current_list.append(path)
        artifacts[key] = current_list

        supabase_client.table("workflows").update(
            {"artifacts": artifacts}
        ).eq("id", workflow_id).execute()

        logger.info(f"[ARTIFACTS] Recorded artifact key={key} path={path} for workflow={workflow_id}")

    except Exception as e:
        logger.error(
            f"[ARTIFACTS] Failed to append artifact record for workflow={workflow_id}, "
            f"key={key}, path={path}: {e}"
        )


def upload_artifact_generic(
    *,
    local_path: str,
    user_id: Optional[str],
    workflow_id: str,
    agent_label: str,
) -> Optional[str]:
    """
    Upload a local file into the 'artifacts' bucket and return the storage path
    that the frontend can use with createSignedUrl.

    Returns:
      "user_id/workflows/<workflow_id>/<agent_label>/<filename>"  (string)
      or None on failure.
    """
    if not os.path.exists(local_path):
        logger.error(f"[ARTIFACTS] Local file does not exist: {local_path}")
        return None

    # Fallback to "anonymous" if user_id is missing
    user_segment = user_id or "anonymous"

    filename = os.path.basename(local_path)
    bucket_path = f"{user_segment}/workflows/{workflow_id}/{agent_label}/{filename}"

    try:
        logger.info(
            f"[ARTIFACTS] Uploading file {local_path} -> bucket 'artifacts' path={bucket_path}"
        )
        with open(local_path, "rb") as f:
            supabase_client.storage.from_("artifacts").upload(
                file=f,
                path=bucket_path,
                file_options={"cache-control": "3600", "upsert": True},
            )

        logger.info(f"[ARTIFACTS] Upload success path={bucket_path}")
        return bucket_path

    except Exception as e:
        logger.error(
            f"[ARTIFACTS] Failed to upload artifact {local_path} "
            f"for workflow_id={workflow_id}, agent_label={agent_label}: {e}"
        )
        return None

