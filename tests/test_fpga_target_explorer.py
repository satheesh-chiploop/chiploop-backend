from agents.fpga import fpga_target_explorer_agent as explorer
import json


def test_capacity_preflight_rejects_impossible_io_before_place_and_route():
    synthesis = {
        "logical_cells_used": 1457,
        "block_ram_blocks_used": 0,
        "cell_type_counts": {"LUT4": 1044, "TRELLIS_FF": 413},
    }
    board = {"resources": {"logic_cells": 84000, "io_cells": 365, "block_ram_blocks": 208, "dsp_blocks": 156}}

    result = explorer._capacity_preflight(synthesis, board, 370)

    assert result["status"] == "reject"
    assert result["checks"]["logic_cells"]["status"] == "pass"
    assert result["checks"]["io_cells"] == {"required": 370, "available": 365, "status": "fail"}
    assert result["failure_reasons"] == ["io_cells requires 370, board provides 365"]


def test_explorer_does_not_run_pnr_for_preflight_rejected_board(monkeypatch):
    synth_calls = []
    pnr_calls = []
    monkeypatch.setattr(explorer, "_run_synthesis", lambda *args: synth_calls.append(args) or {
        "status": "completed", "strategy": "baseline", "netlist": "demo.json",
        "logical_cells_used": 1457, "block_ram_blocks_used": 0,
        "cell_type_counts": {"LUT4": 1044, "TRELLIS_FF": 413},
    })
    monkeypatch.setattr(explorer, "_run_pnr", lambda *args, **_kwargs: pnr_calls.append(args) or {})
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "candidate_boards": ["orangecrab_ecp5_85f"],
        "fpga": {"top_module": "adaptive_aero_control_top", "rtl_files": ["top.sv"]},
        "fpga_explorer_io_mapping": {"top_level_ports": [f"io[{index}]" for index in range(370)]},
    }

    explorer.run_agent(state)

    assert synth_calls == []
    assert pnr_calls == []
    assert state["fpga_target_explorer"]["preflight_rejected_count"] == 1
    assert state["fpga_target_explorer"]["results"][0]["status"] == "capacity_rejected"


def _board(board, fmax, available, used, met=True):
    return {
        "board": board,
        "target_met": met,
        "median_frequency_mhz": fmax,
        "best_frequency_mhz": fmax,
        "timing_pass_rate": 1.0 if met else 0.0,
        "resource_headroom_percent": round((1 - used / available) * 100, 3),
        "logic_cells_available": available,
    }


def test_recommendations_generate_all_profiles():
    results = [
        _board("small", 82, 5280, 3500),
        _board("fast", 140, 44000, 5000),
        _board("growth", 115, 84000, 5000),
    ]

    recommendations = explorer._recommend(results)

    assert recommendations["best_low_cost"] == "small"
    assert recommendations["best_performance"] == "fast"
    assert recommendations["best_for_growth"] == "growth"
    assert set(recommendations) == explorer.PROFILE_KEYS


def test_recommendations_keep_target_met_board_when_pin_mapping_is_incomplete():
    result = _board("timing_fit", 51.883, 25000, 4314)
    result.update({"programming_ready": False, "unmapped_ports": ["spi_sclk", "spi_cs_n"]})

    recommendations = explorer._recommend([result])
    details = explorer._recommendation_details([result], recommendations, 20)

    assert set(recommendations.values()) == {"timing_fit"}
    assert details["best_overall"]["programming_ready"] is False
    assert details["best_overall"]["unmapped_ports"] == ["spi_sclk", "spi_cs_n"]
    assert "Add verified mappings" in details["best_overall"]["next_step"]


def test_automatic_implementation_can_filter_to_programming_ready_target():
    unmapped = _board("larger_unmapped", 140, 84000, 5000)
    unmapped["programming_ready"] = False
    mapped = _board("mapped_fit", 90, 44000, 5000)
    mapped["programming_ready"] = True

    implementation = [item for item in [unmapped, mapped] if item.get("target_met") and item.get("programming_ready")]
    recommendations = explorer._recommend(implementation)

    assert set(recommendations.values()) == {"mapped_fit"}


def test_no_automatic_implementation_target_when_all_board_pin_maps_are_incomplete():
    boards = []
    for name in ("ice40_unmapped", "ecp5_unmapped", "nexus_unmapped", "gowin_unmapped"):
        item = _board(name, 100, 44000, 5000)
        item["programming_ready"] = False
        boards.append(item)

    implementation = [item for item in boards if item.get("target_met") and item.get("programming_ready")]

    assert explorer._recommend(implementation) == {key: None for key in explorer.PROFILE_KEYS}


def test_frequency_relaxation_only_after_target_miss():
    board = {"label": "Demo", "family": "ice40", "device": "up5k", "package": "sg48", "resources": {"logic_cells": 5280}}
    pnr = [{"status": "completed", "seed": 1, "max_frequency_mhz": 68, "logic_cells_used": 3000, "logic_cells_available": 5280}]

    missed = explorer._summarize_board("demo", board, [{"status": "completed"}, {"status": "completed"}], pnr, 75)
    passed = explorer._summarize_board("demo", board, [{"status": "completed"}], pnr, 60)

    assert missed["target_met"] is False
    assert missed["closure_used"] is True
    assert missed["frequency_relaxation"]["recommended_mhz"] == 61.2
    assert passed["target_met"] is True
    assert passed["frequency_relaxation"]["recommended_mhz"] is None


def test_source_memory_gate_distinguishes_optimized_away_from_ff_mapping(tmp_path):
    netlist = tmp_path / "synth.json"
    netlist.write_text(json.dumps({"modules": {"top": {"cells": {"state_ff": {}}}}}), encoding="utf-8")
    intent = {"estimated_bits": 16384, "declarations": [{"name": "history_mem"}]}

    assert explorer._source_memory_optimized_away(str(netlist), intent, {"flip_flops": 1572}) is True

    netlist.write_text(json.dumps({"modules": {"top": {"cells": {"history_mem[0]": {}}}}}), encoding="utf-8")
    assert explorer._source_memory_optimized_away(str(netlist), intent, {"flip_flops": 16384}) is False


def test_allowed_frequency_relaxation_continues_with_programming_ready_board(monkeypatch):
    monkeypatch.setattr(explorer, "_run_synthesis", lambda *_args, **_kwargs: {
        "status": "completed", "strategy": "baseline", "netlist": "demo.json",
        "logical_cells_used": 3000, "block_ram_blocks_used": 0,
    })
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, _board, cfg, _synth, seed, effort: {
        "status": "completed", "seed": seed, "effort": effort,
        "max_frequency_mhz": 44.0, "timing_met": False,
        "logic_cells_used": 3000, "logic_cells_available": cfg["resources"]["logic_cells"],
    })
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {
        "workflow_id": "wf", "target_frequency_mhz": 50,
        "allow_frequency_relaxation": True,
        "candidate_boards": ["ulx3s_ecp5_45f"],
        "fpga": {"top_module": "top", "rtl_files": ["top.sv"]},
        "fpga_explorer_io_mapping": {
            "mappings": [{"board": "ulx3s_ecp5_45f", "programming_ready": True, "mapped_ports": ["clk"], "unmapped_ports": []}],
        },
    }

    explorer.run_agent(state)
    continuation = state["fpga_target_explorer"]["continuation"]

    assert continuation["selected_board"] == "ulx3s_ecp5_45f"
    assert continuation["selection_mode"] == "relaxed_frequency"
    assert continuation["requested_target_frequency_mhz"] == 50
    assert continuation["target_frequency_mhz"] == 39.6
    assert continuation["blocked_reason"] is None


def test_explorer_reuses_identical_implementation_targets(monkeypatch):
    synth_calls = []
    pnr_calls = []
    monkeypatch.setattr(explorer, "CANDIDATE_BOARDS", ["icebreaker", "upduino_v3"])
    monkeypatch.setattr(explorer, "_run_synthesis", lambda _state, board, _cfg, strategy: synth_calls.append((board, strategy)) or {"status": "completed", "strategy": strategy, "netlist": "demo.json"})
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, board, cfg, synth, seed, effort: pnr_calls.append((board, seed)) or {"status": "completed", "seed": seed, "effort": effort, "max_frequency_mhz": 90, "timing_met": True, "logic_cells_used": 3000, "logic_cells_available": cfg["resources"]["logic_cells"]})
    published = {}
    monkeypatch.setattr(explorer, "publish_json", lambda _state, _agent, _subdir, _name, data: published.update(data))
    state = {"workflow_id": "wf", "target_frequency_mhz": 75, "fpga": {"top_module": "top", "rtl_files": ["top.sv"]}}

    explorer.run_agent(state)

    assert synth_calls == [("icebreaker", "baseline")]
    assert len(pnr_calls) == 1
    assert published["candidate_count"] == 2
    assert published["unique_implementation_count"] == 1
    assert published["results"][1]["reused_implementation_from"] == "icebreaker"


def test_workflow_and_frontend_contracts_are_registered():
    from pathlib import Path
    root = Path(__file__).parents[2]
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")
    page = (root / "frontend" / "app" / "apps" / "fpga-target-explorer" / "page.tsx").read_text(encoding="utf-8")
    dashboard = (root / "frontend" / "components" / "WorkflowEvidenceDashboard.tsx").read_text(encoding="utf-8")

    assert '"FPGA_Target_Explorer": FPGA_TARGET_EXPLORER_DEFINITION' in main
    assert '@app.post("/apps/fpga/target-explorer/run")' in main
    template = (root / "frontend" / "app" / "apps" / "digital-review" / "_DigitalReviewAppTemplate.tsx").read_text(encoding="utf-8")
    migration = (root / "backend" / "supabase" / "migrations" / "phase_20260727_fpga_target_explorer.sql").read_text(encoding="utf-8")
    assert 'dashboardStage="fpga_target_explorer"' in page
    assert 'fields={["source", "intent", "rtl", "frequency", "recommendation", "notes"]}' in page
    assert 'candidate_boards: fpgaMode === "target-explorer" ? candidateBoards : undefined' in template
    assert "Upload design intent" in template
    assert 'useState("1")' in template
    assert "baseline_seed_count" in migration and "closure_seed_count" in migration
    assert "FPGA RTL Quality Gate Agent" in migration
    assert "Best Low-Cost Variant" in dashboard
    assert "Continue with this board" in dashboard


def test_explorer_honors_selected_boards_and_emits_progress(monkeypatch):
    progress = []
    synth_calls = []
    monkeypatch.setattr(explorer, "_run_synthesis", lambda _state, board, _cfg, strategy: synth_calls.append((board, strategy)) or {"status": "completed", "strategy": strategy, "netlist": "demo.json"})
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, _board, cfg, _synth, seed, effort: {"status": "completed", "seed": seed, "effort": effort, "max_frequency_mhz": 90, "timing_met": True, "logic_cells_used": 3000, "logic_cells_available": cfg["resources"]["logic_cells"]})
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {"workflow_id": "wf", "target_frequency_mhz": 75, "candidate_boards": ["ice40_hx8k_breakout"], "_progress_callback": progress.append, "fpga": {"top_module": "top", "rtl_files": ["top.sv"]}}

    explorer.run_agent(state)

    assert synth_calls == [("ice40_hx8k_breakout", "baseline")]
    assert state["fpga_target_explorer"]["candidate_count"] == 1
    assert any("baseline P&R 1/1 (seed 1) started" in line for line in progress)
    assert any("Exploration complete" in line for line in progress)


def test_explorer_skips_closure_when_no_route_completes(monkeypatch):
    synth_calls = []
    pnr_calls = []
    progress = []
    monkeypatch.setattr(explorer, "_run_synthesis", lambda _state, board, _cfg, strategy: synth_calls.append((board, strategy)) or {"status": "completed", "strategy": strategy, "netlist": "demo.json"})
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, board, _cfg, _synth, seed, effort: pnr_calls.append((board, seed, effort)) or {"status": "failed", "seed": seed, "effort": effort})
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {"workflow_id": "wf", "candidate_boards": ["icestick"], "_progress_callback": progress.append, "fpga": {"top_module": "top", "rtl_files": ["top.sv"]}}

    explorer.run_agent(state)

    assert synth_calls == [("icestick", "baseline")]
    assert [seed for _board, seed, _effort in pnr_calls] == [1]
    assert any("closure seeds skipped" in line for line in progress)


def test_explorer_honors_user_seed_counts_and_keeps_closure_conditional(monkeypatch):
    pnr_calls = []
    monkeypatch.setattr(explorer, "_run_synthesis", lambda _state, _board, _cfg, strategy: {"status": "completed", "strategy": strategy, "netlist": "demo.json"})
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, _board, cfg, _synth, seed, effort: pnr_calls.append((seed, effort)) or {"status": "completed", "seed": seed, "effort": effort, "max_frequency_mhz": 50, "timing_met": False, "logic_cells_used": 3000, "logic_cells_available": cfg["resources"]["logic_cells"]})
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {"workflow_id": "wf", "target_frequency_mhz": 75, "baseline_seed_count": 2, "closure_seed_count": 2, "closure_near_miss_ratio": 0.6, "candidate_boards": ["ice40_hx8k_breakout"], "fpga": {"top_module": "top", "rtl_files": ["top.sv"]}}

    explorer.run_agent(state)

    assert pnr_calls == [(1, "balanced"), (2, "balanced"), (3, "advanced"), (4, "advanced")]
    assert state["fpga_target_explorer"]["seed_policy"] == {"baseline_seed_count": 2, "closure_seed_count": 2, "closure_is_conditional": True}


def test_explorer_skips_expensive_closure_for_large_timing_miss(monkeypatch):
    pnr_calls = []
    progress = []
    monkeypatch.setattr(explorer, "_run_synthesis", lambda _state, _board, _cfg, strategy: {"status": "completed", "strategy": strategy, "netlist": "demo.json"})
    monkeypatch.setattr(explorer, "_run_pnr", lambda _state, _board, cfg, _synth, seed, effort: pnr_calls.append((seed, effort)) or {"status": "completed", "seed": seed, "effort": effort, "max_frequency_mhz": 50, "timing_met": False, "logic_cells_used": 3000, "logic_cells_available": cfg["resources"]["logic_cells"]})
    monkeypatch.setattr(explorer, "publish_json", lambda *_args: None)
    state = {"workflow_id": "wf", "target_frequency_mhz": 75, "candidate_boards": ["ice40_hx8k_breakout"], "_progress_callback": progress.append, "fpga": {"top_module": "top", "rtl_files": ["top.sv"]}}

    explorer.run_agent(state)

    assert pnr_calls == [(1, "balanced")]
    assert state["fpga_target_explorer"]["results"][0]["frequency_relaxation"]["recommended_mhz"] == 45.0
    assert any("below the 85% near-miss threshold" in line for line in progress)


def test_vendor_catalog_prefixes_and_support_tiers():
    from agents.fpga.fpga_common import BOARD_REGISTRY, board_config

    runnable = ["certus_nx_versa_40", "crosslink_nx_eval_40", "gowin_tang_nano_9k", "gowin_tang_nano_20k", "gowin_tang_primer_20k"]
    for key in runnable:
        target = BOARD_REGISTRY[key]
        assert target["label"].lower().startswith(target["vendor"])
        assert target["support_tier"] in {"beta", "experimental"}
        assert target["segments"]
        assert board_config({"board": key})["supported"] is True

    assert board_config({"board": "machxo5_nx_65t"})["supported"] is False
    assert board_config({"board": "gowin_gw3a_20k"})["supported"] is False
    assert board_config({"board": "certuspro_nx_versa_100"})["supported"] is False
    assert board_config({"board": "gowin_gw5a_25_starter"})["supported"] is False


def test_open_source_architecture_commands(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(explorer, "_nextpnr_help", lambda _tool: "--freq --timing-allow-fail")
    monkeypatch.setattr(explorer, "_nextpnr_version", lambda _tool: "test")
    monkeypatch.setattr(explorer, "run_cmd", lambda cmd, **_kwargs: commands.append(cmd) or {"ok": False, "cmd": cmd})
    state = {"workflow_id": "wf", "workflow_dir": str(tmp_path), "target_frequency_mhz": 75}

    for key in ("certus_nx_versa_40", "gowin_tang_nano_9k"):
        board = explorer.BOARD_REGISTRY[key]
        explorer._run_pnr(state, key, board, {"strategy": "baseline", "netlist": "demo.json"}, 1, "balanced")

    assert commands[0][0] == "nextpnr-nexus"
    assert "--fasm" in commands[0]
    assert commands[1][0] == "nextpnr-himbaechel"
    assert "family=GW1N-9C" in commands[1]
    assert "--write" in commands[1]


def test_frontend_and_supabase_share_vendor_target_catalog():
    from pathlib import Path
    root = Path(__file__).parents[2]
    frontend_catalog = (root / "frontend" / "lib" / "fpgaTargets.ts").read_text(encoding="utf-8")
    migration = (root / "backend" / "supabase" / "migrations" / "phase_20260728_fpga_vendor_open_source_targets.sql").read_text(encoding="utf-8")
    template = (root / "frontend" / "app" / "apps" / "digital-review" / "_DigitalReviewAppTemplate.tsx").read_text(encoding="utf-8")

    for key in ("certus_nx_versa_40", "crosslink_nx_eval_40", "certuspro_nx_versa_100", "gowin_tang_nano_9k", "gowin_tang_nano_20k", "gowin_tang_primer_20k", "gowin_gw5a_25_starter"):
        assert key in frontend_catalog
        assert key in migration
    assert "FPGA_TARGET_OPTIONS.map" in template
    assert "PCF / LPF / CST" in template


def test_explorer_rejects_unavailable_board_even_from_direct_api_input(tmp_path):
    state = {
        "workflow_id": "wf-unavailable",
        "workflow_dir": str(tmp_path),
        "target_frequency_mhz": 75,
        "candidate_boards": ["certuspro_nx_versa_100"],
        "fpga": {"rtl_files": ["demo.sv"], "top_module": "demo"},
    }
    import pytest
    with pytest.raises(RuntimeError, match="Select at least one supported FPGA board/device"):
        explorer.run_agent(state)


def test_recommendation_details_explain_timing_margin_and_next_step():
    results = [{
        "board": "demo", "label": "Demo Board", "target_met": True,
        "best_frequency_mhz": 90.0, "timing_margin_percent": 20.0,
        "resource_headroom_percent": 70.0, "toolchain_confidence": "qualified",
        "constraint_confidence": "exploration_only",
    }]

    details = explorer._recommendation_details(results, {"best_overall": "demo"}, 75.0)

    assert details["best_overall"]["why"] == "Meets 75 MHz with 20.0% timing margin and 70.0% logic headroom."
    assert details["best_overall"]["constraint_confidence"] == "exploration_only"
    assert "FPGA Prototyping" in details["best_overall"]["next_step"]


def test_explorer_frontend_carries_winning_configuration_and_provenance():
    from pathlib import Path
    root = Path(__file__).parents[2]
    dashboard = (root / "frontend" / "components" / "WorkflowEvidenceDashboard.tsx").read_text(encoding="utf-8")
    template = (root / "frontend" / "app" / "apps" / "digital-review" / "_DigitalReviewAppTemplate.tsx").read_text(encoding="utf-8")
    main = (root / "backend" / "main.py").read_text(encoding="utf-8")

    assert "explorerWinningConfiguration" in dashboard
    assert "explorerSourceWorkflowId" in dashboard
    assert "FPGA Prototyping will rerun verification and implementation" in dashboard
    assert "explorer_winning_configuration" in template
    assert "fpga_nextpnr_seed" in template
    assert "explorer_winning_configuration: Optional[Dict[str, Any]]" in main


def test_fpga_prototyping_applies_explorer_winning_seed_and_strategy():
    from pathlib import Path
    main = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    endpoint = main[main.index("async def apps_fpga_bitstream_run"):main.index('@app.post("/apps/fpga2rtl/run")')]

    assert 'data["fpga_nextpnr_seed"] = explorer_winner.get("seed")' in endpoint
    assert 'data["fpga_yosys_retime"] = True' in endpoint
    assert 'data["fpga_yosys_flatten"] = True' in endpoint
