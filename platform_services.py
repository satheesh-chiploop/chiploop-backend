import os
from typing import Any, Dict


def platform_services_summary() -> Dict[str, Any]:
    database_provider = os.getenv("CHIPLOOP_DATABASE_PROVIDER", "supabase")
    storage_provider = os.getenv("CHIPLOOP_STORAGE_PROVIDER", "supabase")
    auth_provider = os.getenv("CHIPLOOP_AUTH_PROVIDER", "supabase")
    services = {
        "database": {
            "provider": database_provider,
            "configured": bool(os.getenv("SUPABASE_URL") or os.getenv("DATABASE_URL")),
            "supported": database_provider in {"supabase", "postgrest", "postgres", "postgresql"},
        },
        "storage": {
            "provider": storage_provider,
            "configured": bool(
                os.getenv("SUPABASE_URL")
                or os.getenv("CHIPLOOP_STORAGE_BUCKET")
                or os.getenv("CHIPLOOP_PRIVATE_ARTIFACT_ROOT")
                or os.getenv("AZURE_STORAGE_CONNECTION_STRING")
                or os.getenv("AZURE_STORAGE_ACCOUNT_URL")
            ) or storage_provider in {"local", "local_fs", "filesystem"},
            "supported": storage_provider in {"supabase", "local", "local_fs", "filesystem", "s3", "minio", "azure_blob", "azure"},
        },
        "auth": {
            "provider": auth_provider,
            "configured": bool(
                os.getenv("SUPABASE_URL")
                or os.getenv("CHIPLOOP_OIDC_ISSUER")
                or os.getenv("CHIPLOOP_AUTH_PROVIDER") == "disabled"
            ),
            "supported": auth_provider in {"supabase", "oidc", "okta", "auth0", "azure_ad", "entra_id", "disabled"},
        },
    }
    frontend_provider = os.getenv("CHIPLOOP_FRONTEND_PLATFORM_PROVIDER") or (
        "supabase_direct" if database_provider == "supabase" and auth_provider == "supabase" else "backend_platform_api"
    )
    services["frontend"] = {
        "provider": frontend_provider,
        "configured": bool(os.getenv("NEXT_PUBLIC_SUPABASE_URL") or os.getenv("SUPABASE_URL") or os.getenv("DATABASE_URL")),
        "supported": frontend_provider in {"supabase_direct", "backend_platform_api"},
    }
    return services
