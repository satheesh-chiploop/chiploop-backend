import os

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.embedded import _embedded_common as common
from agents.embedded import embedded_elf_build_agent


def test_write_artifact_materializes_file_for_downstream_execution(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        common,
        "save_text_artifact_and_record",
        lambda **kwargs: recorded.append(kwargs),
    )
    state = {"workflow_id": "embedded-wf", "workflow_dir": str(tmp_path)}

    result = common.write_artifact(state, "firmware/build/Cargo.toml", "[package]\n")

    assert result == "firmware/build/Cargo.toml"
    assert (tmp_path / "firmware" / "build" / "Cargo.toml").read_text(encoding="utf-8") == "[package]\n"
    assert recorded[0]["subdir"] == "firmware"
    assert recorded[0]["filename"] == "build/Cargo.toml"


def test_write_artifact_rejects_path_outside_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "save_text_artifact_and_record", lambda **_kwargs: None)
    state = {"workflow_id": "embedded-wf", "workflow_dir": str(tmp_path)}

    with pytest.raises(RuntimeError, match="escapes workflow directory"):
        common.write_artifact(state, "../outside.txt", "not allowed")


def test_default_linux_cargo_config_supports_no_std_entrypoint():
    config = embedded_elf_build_agent._default_cargo_config("x86_64-unknown-linux-gnu")

    assert 'target = "x86_64-unknown-linux-gnu"' in config
    assert 'link-arg=-nostartfiles' in config
