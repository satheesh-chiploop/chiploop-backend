from agents.fpga import fpga_target_explorer_agent as explorer


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
    assert len(pnr_calls) == 3
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
    assert 'dashboardStage="fpga_target_explorer"' in page
    assert "Best Low-Cost Variant" in dashboard
    assert "Continue with this board" in dashboard
