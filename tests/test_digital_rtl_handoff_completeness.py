import os
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_rtl_handoff_ingest_agent import _complete_local_rtl_module_closure, _declared_modules, run_agent


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


def test_handoff_module_closure_adds_required_upstream_macro(tmp_path: Path):
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    top = work / "wrapper.v"
    macro = source / "qualified_sram.v"
    top.write_text("module wrapper(input clk);\nqualified_sram u_mem(.clk(clk));\nendmodule\n", encoding="utf-8")
    macro.write_text("module qualified_sram(input clk); endmodule\n", encoding="utf-8")

    files, unresolved = _complete_local_rtl_module_closure([str(top)], [source], str(work))

    assert unresolved == []
    assert {Path(path).name for path in files} == {"wrapper.v", "qualified_sram.v"}


def test_handoff_module_closure_reports_missing_nonprimitive_module(tmp_path: Path):
    top = tmp_path / "top.v"
    top.write_text("module top(input a, output y);\nmissing_child u(.a(a));\nbuf b(y, a);\nendmodule\n", encoding="utf-8")

    _files, unresolved = _complete_local_rtl_module_closure([str(top)], [], str(tmp_path))

    assert unresolved == ["missing_child"]
