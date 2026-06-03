import os
from pathlib import Path
from typing import Dict


VALID_ARTIFACT_POLICIES = {
    "full_sync",
    "summary_only",
    "manifest_only",
    "private_storage",
    "full_private",
    "customer_cloud_storage",
}

SUMMARY_SUFFIXES = (".json", ".md", ".txt", ".log", ".csv", ".rpt")
MANIFEST_NAMES = ("manifest", "summary", "report", "dashboard", "metrics", "results")


def active_artifact_policy() -> str:
    policy = os.getenv("CHIPLOOP_ARTIFACT_POLICY", "full_sync").strip().lower()
    return policy if policy in VALID_ARTIFACT_POLICIES else "full_sync"


def artifact_may_sync(filename: str, policy: str | None = None) -> bool:
    policy = policy or active_artifact_policy()
    lowered = (filename or "").lower()
    if policy == "full_sync":
        return True
    if policy == "customer_cloud_storage":
        return True
    if policy in {"private_storage", "full_private"}:
        return False
    if policy == "summary_only":
        return lowered.endswith(SUMMARY_SUFFIXES)
    if policy == "manifest_only":
        return any(token in lowered for token in MANIFEST_NAMES) and lowered.endswith(SUMMARY_SUFFIXES)
    return False


def write_private_artifact(workflow_id: str, subdir: str, filename: str, content: str) -> str:
    root = Path(os.getenv("CHIPLOOP_PRIVATE_ARTIFACT_ROOT", "private_artifacts"))
    path = root / "workflows" / workflow_id / subdir.strip("/") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path.resolve())


def artifact_policy_summary() -> Dict[str, str]:
    return {
        "policy": active_artifact_policy(),
        "private_artifact_root": os.getenv("CHIPLOOP_PRIVATE_ARTIFACT_ROOT", "private_artifacts"),
    }
