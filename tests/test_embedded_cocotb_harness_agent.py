import os

import json
import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.embedded import embedded_cocotb_harness_agent as agent


def test_makefile_keeps_verilator_lint_warnings_nonfatal():
    text = agent._makefile(
        "pwm_controller",
        ["digital/rtl/pwm_controller.v"],
        "test_firmware_smoke",
    )

    assert "SIM ?= verilator" in text
    assert "EXTRA_ARGS += -Wno-fatal -Wno-CASEINCOMPLETE" in text
    assert "digital/rtl/pwm_controller.v" in text
    assert "include $(shell cocotb-config --makefiles)/Makefile.sim" in text


def test_identical_packaging_duplicates_select_preferred_source(tmp_path):
    canonical = tmp_path / "digital" / "rtl" / "leaf.v"
    imported = tmp_path / "system" / "imported_rtl" / "leaf.v"
    canonical.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    rtl = b"module leaf(input wire clk); endmodule\n"
    canonical.write_bytes(rtl)
    imported.write_bytes(rtl)

    selected = agent._select_equivalent_candidate(
        str(tmp_path),
        ["system/imported_rtl/leaf.v", "digital/rtl/leaf.v"],
        ["digital/rtl/leaf.v", "system/imported_rtl/leaf.v"],
    )

    assert selected == "digital/rtl/leaf.v"


def test_divergent_duplicate_definitions_remain_ambiguous(tmp_path):
    first = tmp_path / "digital" / "rtl" / "leaf.v"
    second = tmp_path / "system" / "imported_rtl" / "leaf.v"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("module leaf; endmodule\n", encoding="utf-8")
    second.write_text("module leaf; wire changed; endmodule\n", encoding="utf-8")

    assert agent._select_equivalent_candidate(
        str(tmp_path),
        ["digital/rtl/leaf.v", "system/imported_rtl/leaf.v"],
    ) is None


def test_required_source_resolution_collapses_identical_packaging_copies(tmp_path, monkeypatch):
    canonical_dir = tmp_path / "digital" / "rtl"
    imported_dir = tmp_path / "system" / "imported_rtl"
    canonical_dir.mkdir(parents=True)
    imported_dir.mkdir(parents=True)
    top_text = "module top(input wire clk);\nleaf u_leaf (\n  .clk(clk)\n);\nendmodule\n"
    leaf_text = "module leaf(input wire clk); endmodule\n"
    (canonical_dir / "top.v").write_text(top_text, encoding="utf-8")
    (canonical_dir / "leaf.v").write_text(leaf_text, encoding="utf-8")
    (imported_dir / "leaf.v").write_text(leaf_text, encoding="utf-8")
    captured = {}

    def capture_artifact(_state, path, content, **_kwargs):
        captured[path] = content

    monkeypatch.setattr(agent, "write_artifact", capture_artifact)
    sources = agent._resolve_required_verilog_sources(
        str(tmp_path),
        "digital/rtl/top.v",
        top_text,
        {
            "rtl_inputs": [
                "digital/rtl/leaf.v",
                "system/imported_rtl/leaf.v",
            ]
        },
    )

    assert sources == ["digital/rtl/top.v", "digital/rtl/leaf.v"]
    debug = json.loads(captured[agent.RTL_RESOLUTION_DEBUG_PATH])
    assert debug["ambiguous_modules"] == {}
    assert debug["equivalent_duplicate_modules"]["leaf"]["selected"] == "digital/rtl/leaf.v"


def test_required_source_resolution_still_rejects_divergent_copies(tmp_path, monkeypatch):
    canonical_dir = tmp_path / "digital" / "rtl"
    imported_dir = tmp_path / "system" / "imported_rtl"
    canonical_dir.mkdir(parents=True)
    imported_dir.mkdir(parents=True)
    top_text = "module top(input wire clk);\nleaf u_leaf (\n  .clk(clk)\n);\nendmodule\n"
    (canonical_dir / "top.v").write_text(top_text, encoding="utf-8")
    (canonical_dir / "leaf.v").write_text("module leaf(input wire clk); endmodule\n", encoding="utf-8")
    (imported_dir / "leaf.v").write_text("module leaf(input wire clk); wire changed; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(agent, "write_artifact", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Ambiguous RTL definition files.*leaf"):
        agent._resolve_required_verilog_sources(
            str(tmp_path),
            "digital/rtl/top.v",
            top_text,
            {"rtl_inputs": ["digital/rtl/leaf.v", "system/imported_rtl/leaf.v"]},
        )
