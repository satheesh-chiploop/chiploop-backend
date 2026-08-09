import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_synthesis_readiness_agent as readiness


def test_dedupes_byte_identical_rtl_workflow_copies(tmp_path: Path):
    source = "module top(input logic clk, output logic q); always_ff @(posedge clk) q <= ~q; endmodule\n"
    upstream = tmp_path / "upstream" / "top.sv"
    pass2 = tmp_path / "pass2" / "top.sv"
    upstream.parent.mkdir()
    pass2.parent.mkdir()
    upstream.write_text(source, encoding="utf-8")
    pass2.write_text(source, encoding="utf-8")

    unique, ignored = readiness._dedupe_identical_rtl_files([str(upstream), str(pass2)])

    assert unique == [str(upstream.resolve())]
    assert ignored == [{
        "path": str(pass2.resolve()),
        "matches": str(upstream.resolve()),
        "reason": "duplicate_content",
    }]


def test_keeps_different_rtl_files_with_same_filename(tmp_path: Path):
    first = tmp_path / "a" / "top.sv"
    second = tmp_path / "b" / "top.sv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("module top; endmodule\n", encoding="utf-8")
    second.write_text("module top(input clk); endmodule\n", encoding="utf-8")

    unique, ignored = readiness._dedupe_identical_rtl_files([str(first), str(second)])

    assert unique == [str(first.resolve()), str(second.resolve())]
    assert ignored == []
