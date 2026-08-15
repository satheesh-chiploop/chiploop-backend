import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_package_audit_agent as audit


def _state(tmp_path, include_adapter_files=True):
    adapter_path = "system/software/adapter/adaptive_aero_control_adapter"
    files = [f"{adapter_path}/Cargo.toml", f"{adapter_path}/src/lib.rs"] if include_adapter_files else []
    restored_root = tmp_path / "restored_system_software"
    for path in files:
        target = restored_root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated", encoding="utf-8")
    return {
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "_fail_fast_on_agent_error": True,
        "system_software_validation_manifest": {
            "discovered_assets": {
                "adapter_manifest": {
                    "exists": True,
                    "resolved_path": "backend/workflows/source/system/software/adapter/system_software_adapter_manifest.json",
                }
            },
            "validation_inputs": {"required_assets": [], "optional_assets": []},
        },
        "system_software_adapter_manifest": {
            "adapter_crate": "adaptive_aero_control_adapter",
            "adapter_path": adapter_path,
        },
        "system_software_validation_local_root": str(restored_root),
        # Supabase package paths can be storage-qualified; audit must verify
        # the restored contract-relative adapter path instead of its manifest parent.
        "system_software_package": {
            "files": [f"backend/workflows/source/{path}" for path in files],
            "artifact_count": len(files),
        },
        "system_software_validation_package_file_checks": [],
    }


def test_audit_uses_adapter_contract_path_not_storage_manifest_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_record_text", lambda *_args, **_kwargs: None)
    state = _state(tmp_path)

    result = audit.run_agent(state)

    report = result["system_software_package_audit"]
    assert report["package_status"] == "complete"
    assert report["required_adapter_package_files"] == [
        "system/software/adapter/adaptive_aero_control_adapter/Cargo.toml",
        "system/software/adapter/adaptive_aero_control_adapter/src/lib.rs",
    ]
    assert report["missing_required_adapter_package_files"] == []


def test_audit_still_fails_when_declared_adapter_files_are_really_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_record_text", lambda *_args, **_kwargs: None)
    state = _state(tmp_path, include_adapter_files=False)

    try:
        audit.run_agent(state)
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
