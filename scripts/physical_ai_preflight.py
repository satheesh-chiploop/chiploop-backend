"""Production preflight for the Physical AI reference journey.

This is read-only. It never starts a workflow, uploads an artifact, programs an
FPGA, or enables a motor. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY before
running it. Optionally set CHIPLOOP_API_URL and CHIPLOOP_ACCESS_TOKEN to verify
the authenticated deployed model-catalog endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _fail(message: str) -> None:
    print(f"FAIL  {message}")


def _pass(message: str) -> None:
    print(f"PASS  {message}")


def _require_environment() -> tuple[str, str]:
    url = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    missing = [name for name, value in (("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", key)) if not value]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
    return url, key


def _api_check() -> bool:
    api_url = (os.getenv("CHIPLOOP_API_URL") or "").rstrip("/")
    token = (os.getenv("CHIPLOOP_ACCESS_TOKEN") or "").strip()
    if not api_url or not token:
        print("SKIP  deployed API check (set CHIPLOOP_API_URL and CHIPLOOP_ACCESS_TOKEN)")
        return True
    request = urllib.request.Request(
        f"{api_url}/apps/physical-ai/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        _fail(f"deployed API model catalog: {exc}")
        return False
    ok = body.get("source_of_truth") == "supabase" and any(
        row.get("model_id") == "chiploop.pmsm.dq.v1" for row in body.get("models", [])
    )
    (_pass if ok else _fail)("deployed API returns the Supabase PMSM catalog")
    return ok


def main() -> int:
    try:
        url, key = _require_environment()
        from supabase import create_client
    except Exception as exc:
        _fail(str(exc))
        return 2

    client = create_client(url, key)
    checks: list[tuple[str, callable]] = [
        (
            "PMSM model is ready in physical_ai_models",
            lambda: bool(
                client.table("physical_ai_models")
                .select("model_id,availability,executor")
                .eq("model_id", "chiploop.pmsm.dq.v1")
                .eq("availability", "ready")
                .limit(1)
                .execute()
                .data
            ),
        ),
        (
            "Physical_AI_Loop prebuilt workflow exists",
            lambda: bool(
                client.table("workflows")
                .select("id,name,loop_type,definitions")
                .eq("name", "Physical_AI_Loop")
                .eq("loop_type", "physical_ai")
                .is_("user_id", "null")
                .limit(1)
                .execute()
                .data
            ),
        ),
        ("HEM run persistence is readable", lambda: client.table("hem_runs").select("id").limit(1).execute() is not None),
        ("HEM event persistence is readable", lambda: client.table("hem_run_events").select("id").limit(1).execute() is not None),
        ("runs.artifacts_path is readable", lambda: client.table("runs").select("id,artifacts_path").limit(1).execute() is not None),
        ("workflows.artifacts JSON index is readable", lambda: client.table("workflows").select("id,artifacts").limit(1).execute() is not None),
        ("artifacts Storage bucket is readable", lambda: client.storage.from_(os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")).list("") is not None),
    ]
    failures = 0
    for label, check in checks:
        try:
            ok = bool(check())
        except Exception as exc:
            _fail(f"{label}: {exc}")
            failures += 1
            continue
        (_pass if ok else _fail)(label)
        failures += 0 if ok else 1
    if not _api_check():
        failures += 1
    print(f"\nPhysical AI preflight: {'READY' if failures == 0 else 'NOT READY'} ({failures} failed checks)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
