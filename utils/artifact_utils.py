"""
artifact_utils.py
Generic helper functions for uploading agent outputs (artifacts)
to Supabase Storage and updating the workflows.artifacts JSON field.
"""

import os
from supabase import create_client, Client
from typing import Optional, Dict

# --------------------------------------------------------------------
# 🔧 Initialize Supabase client (service role key for full access)
# --------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Supabase URL or SERVICE KEY missing from environment")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# --------------------------------------------------------------------
# 📦 Upload any file to Supabase Storage
# --------------------------------------------------------------------
def upload_artifact_generic(
    local_path: str, user_id: str, workflow_id: str, agent_label: str
) -> Optional[str]:
    """
    Uploads a local file to the 'artifacts' bucket in Supabase Storage.
    Returns the relative bucket path (without 'artifacts/' prefix) or None on failure.
    """
    try:
        if not os.path.exists(local_path):
            print(f"⚠️ File not found: {local_path}")
            return None

        filename = os.path.basename(local_path)
        bucket_path = f"{user_id}/workflows/{workflow_id}/{agent_label}/{filename}"

        with open(local_path, "rb") as f:
            result = supabase.storage.from_("artifacts").upload(bucket_path, f)

        if isinstance(result, dict) and result.get("error"):
            raise Exception(result["error"]["message"])

        print(f"✅ Uploaded artifact: {bucket_path}")
        return bucket_path
    except Exception as e:
        print(f"❌ Upload failed for {local_path}: {e}")
        return None


# --------------------------------------------------------------------
# 🧱 Append artifact entry to workflows.artifacts JSON field
# --------------------------------------------------------------------
def append_artifact_record(workflow_id: str, key: str, path: str) -> None:
    """
    Inserts or updates a single artifact entry inside the workflows.artifacts JSON.
    Example:
        append_artifact_record('abcd123', 'rtl_file', 'user1/workflows/abcd123/rtl/counter.v')
    """
    try:
        # Fetch existing artifacts
        existing = (
            supabase.table("workflows")
            .select("artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
        )

        artifacts: Dict[str, str] = existing.data.get("artifacts", {}) if existing.data else {}
        artifacts[key] = path  # add or overwrite

        # Push update
        supabase.table("workflows").update({"artifacts": artifacts}).eq("id", workflow_id).execute()
        print(f"🧩 Updated artifacts JSON for workflow {workflow_id} with key '{key}'")
    except Exception as e:
        print(f"❌ Failed to update artifact record: {e}")


# --------------------------------------------------------------------
# 🧹 Optional cleanup
# --------------------------------------------------------------------
def delete_artifact_from_storage(path: str) -> bool:
    """Removes a file from the Supabase 'artifacts' bucket."""
    try:
        result = supabase.storage.from_("artifacts").remove([path])
        print(f"🗑️ Deleted artifact: {path}")
        return True
    except Exception as e:
        print(f"❌ Failed to delete artifact: {e}")
        return False


def reset_workflow_artifacts(workflow_id: str) -> None:
    """Clears the artifacts JSON for a given workflow."""
    try:
        supabase.table("workflows").update({"artifacts": {}}).eq("id", workflow_id).execute()
        print(f"♻️ Reset artifacts for workflow {workflow_id}")
    except Exception as e:
        print(f"❌ Failed to reset artifacts: {e}")
