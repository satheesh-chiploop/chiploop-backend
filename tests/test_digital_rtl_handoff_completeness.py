import os
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_rtl_handoff_ingest_agent import _declared_modules, run_agent


def test_declared_modules_reads_complete_rtl_set(tmp_path: Path):
    child = tmp_path / "child.v"
    top = tmp_path / "top.v"
    child.write_text("module child(input wire a); endmodule\n", encoding="utf-8")
    top.write_text("module top(input wire a); child u_child(.a(a)); endmodule\n", encoding="utf-8")

    assert _declared_modules([str(child), str(top)]) == {"child", "top"}


def test_handoff_rejects_top_missing_from_imported_file_set(tmp_path: Path):
    rtl = tmp_path / "only_child.v"
    rtl.write_text("module child(input wire a); endmodule\n", encoding="utf-8")
    state = {
        "artifact_dir": str(tmp_path),
        "rtl_files": [str(rtl)],
        "top_module": "expected_top",
    }

    with pytest.raises(RuntimeError, match="Top module 'expected_top' is not present"):
        run_agent(state)


def test_handoff_preserves_all_existing_rtl_files(tmp_path: Path):
    child = tmp_path / "child.v"
    top = tmp_path / "top.v"
    child.write_text("module child(input wire a); endmodule\n", encoding="utf-8")
    top.write_text("module top(input wire a); child u_child(.a(a)); endmodule\n", encoding="utf-8")
    state = {
        "artifact_dir": str(tmp_path),
        "rtl_files": [str(child), str(top)],
        "top_module": "top",
    }

    result = run_agent(state)

    assert result["top_module"] == "top"
    assert {Path(path).name for path in result["rtl_files"]} == {"child.v", "top.v"}
