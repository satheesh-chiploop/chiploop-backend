import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_closure_recommendation_agent as recommendation_agent
from agents.digital import digital_closure_rerun_planner_agent as rerun_planner_agent
from agents.digital import digital_coverage_gap_analysis_agent as gap_agent
from agents.digital import digital_failure_triage_agent as triage_agent
from agents.digital import digital_failure_debug_agent as debug_agent
from agents.digital import digital_simulation_execution_agent as execution_agent
from agents.digital import digital_simulation_summary_coverage_agent as summary_agent
from agents.digital import digital_sva_assertions_agent as sva_agent
from agents.digital import digital_behavioral_rtl_repair_agent as behavioral_repair_agent
from agents.digital import digital_closure_iteration_judge_agent as iteration_judge_agent
from agents.digital import digital_verification_handoff_ingest_agent as verification_handoff_agent
from agents.digital import digital_testcase_seed_update_agent as testcase_seed_agent
from agents.digital import digital_verify_closure_ingest_agent as ingest_agent


def _stub_upload(monkeypatch):
    monkeypatch.setattr(ingest_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(gap_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(triage_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(debug_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(recommendation_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(testcase_seed_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(rerun_planner_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)


def test_behavioral_obligations_have_stable_requirement_and_checker_ids():
    spec = {"hierarchy": {"top_module": {
        "name": "controller",
        "responsibilities": ["Expose configuration outputs."],
        "behavior_rules": [
            "When clear_fault is asserted, sticky_fault clears on the next cycle.",
            "Clamp the response before asserting response_valid.",
        ],
    }}}
    obligations = sva_agent._behavioral_obligations(spec)
    assert [item["requirement_id"] for item in obligations] == ["REQ-002", "REQ-003"]
    assert [item["checker_id"] for item in obligations] == ["a_req_002", "a_req_003"]
    assert all(item["verification_method"] == "systemverilog_assertion" for item in obligations)


def test_checker_quality_reads_assertions_with_action_clauses_and_rejects_false_stability():
    sva_spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-001",
        "checker_id": "a_req_001",
        "requirement": "Maintain a synchronous 8-bit counter state.",
    }, {
        "requirement_id": "REQ-002",
        "checker_id": "a_req_002",
        "requirement": "Expose the current counter value for observability.",
    }]}
    sva = """
property p_a_req_001;
  @(posedge clk) 1'b1 |=> $stable(counter_value);
endproperty
a_req_001: assert property(p_a_req_001)
  else $fatal(1, "failed");
property p_a_req_002;
  @(posedge clk) 1'b1 |-> (counter_value == counter_value);
endproperty
a_req_002: assert property(p_a_req_002)
  else $fatal(1, "failed");
"""

    issues = sva_agent._checker_quality_issues(sva, sva_spec)

    assert any(item["requirement_id"] == "REQ-001" and "$stable" in item["issue"] for item in issues)
    assert any(item["requirement_id"] == "REQ-002" and "tautological" in item["issue"] for item in issues)


def test_dynamic_classifier_result_does_not_contradict_sva_generation_contract():
    spec = {"hierarchy": {"top_module": {
        "name": "waveform", "ports": [],
        "responsibilities": ["Generate a programmable waveform from the configured threshold."],
    }}}
    obligation = sva_agent._behavioral_obligations(spec)[0]
    assert obligation["requirement_classification"] == "dynamic_simulation"
    assert obligation["verification_method"] == "systemverilog_assertion"


def test_sva_targets_bind_behavioral_requirements_to_owning_child_module():
    spec = {"hierarchy": {
        "top_module": {"name": "system_top", "ports": [
            {"name": "clk", "direction": "input"},
        ]},
        "modules": [{
            "name": "watchdog", "ports": [
                {"name": "clk", "direction": "input"},
                {"name": "kick", "direction": "input"},
                {"name": "expired", "direction": "output"},
            ],
            "behavior_rules": ["When kick is asserted, expired clears on the next cycle."],
        }],
    }}
    sva_spec = sva_agent._build_sva_spec(spec, "system_top")
    child = next(item for item in sva_spec["verification_targets"] if item["module"] == "watchdog")
    assert {port["name"] for port in child["ports"]} == {"clk", "kick", "expired"}
    assert child["behavioral_obligations"][0]["owner_module"] == "watchdog"
    bind = sva_agent._gen_bind_sv("system_top", "system_top_assertions", sva_spec)
    assert "bind watchdog watchdog_assertions" in bind
    assert ".expired(expired)" in bind


def test_sva_target_preserves_flat_top_module_ports():
    spec = {"top_module": {
        "name": "counter",
        "ports": [
            {"name": "clk", "direction": "input"},
            {"name": "enable", "direction": "input"},
            {"name": "done", "direction": "output"},
        ],
        "behavior_rules": ["When enable is asserted, done pulses on the next cycle."],
    }}
    sva_spec = sva_agent._build_sva_spec(spec, "counter")
    assert len(sva_spec["behavioral_obligations"]) == 1
    assert {port["name"] for port in sva_spec["verification_targets"][0]["ports"]} == {
        "clk", "enable", "done",
    }


def test_sva_completeness_retry_closes_model_omissions(tmp_path, monkeypatch):
    sva_spec = {"behavioral_obligations": [
        {"requirement_id": "REQ-001", "checker_id": "a_req_001", "requirement": "When enable is high, done asserts."},
        {"requirement_id": "REQ-002", "checker_id": "a_req_002", "requirement": "When clear is high, done deasserts."},
    ]}
    incomplete = """module checks(input logic clk, input logic enable, input logic clear, input logic done);
a_req_001: assert property (@(posedge clk) enable |-> done);
c_req_001: cover property (@(posedge clk) enable);
endmodule
"""
    complete = """module checks(input logic clk, input logic enable, input logic clear, input logic done);
a_req_001: assert property (@(posedge clk) enable |-> done);
c_req_001: cover property (@(posedge clk) enable);
a_req_002: assert property (@(posedge clk) clear |-> !done);
c_req_002: cover property (@(posedge clk) clear);
endmodule
"""
    calls = []
    monkeypatch.setattr(sva_agent, "complete_text", lambda prompt, **kwargs: calls.append(prompt) or complete)
    closed, attempts = sva_agent._close_missing_checkers(
        {}, incomplete, str(tmp_path / "sva.log"), sva_spec, state={}
    )
    assert attempts == 2
    assert sva_agent._missing_behavioral_checker_ids(closed, sva_spec) == []
    assert "REQ-002" in calls[0]


def test_sva_completeness_retry_is_bounded_when_model_keeps_omitting(tmp_path, monkeypatch):
    spec = {"behavioral_obligations": [
        {"requirement_id": "REQ-001", "checker_id": "a_req_001", "requirement": "First"},
    ]}
    source = "module checks(input logic clk); endmodule\n"
    monkeypatch.setattr(sva_agent, "complete_text", lambda *args, **kwargs: source)
    closed, attempts = sva_agent._close_missing_checkers(
        {}, source, str(tmp_path / "sva.log"), spec, state={}
    )
    assert attempts == 3
    assert sva_agent._missing_behavioral_checker_ids(closed, spec) == ["REQ-001"]


def test_sva_quality_rejects_overlapping_implication_for_next_edge_behavior():
    spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-010", "checker_id": "a_req_010",
        "requirement": "When enable is high, counter increments on the next rising edge.",
    }]}
    bad = """property p_req_010;
@(posedge clk) enable |-> counter == $past(counter) + 1;
endproperty
a_req_010: assert property (p_req_010);
c_req_010: cover property (@(posedge clk) enable);
"""
    good = bad.replace("|->", "|=>")
    issues = sva_agent._checker_quality_issues(bad, spec)
    assert issues[0]["requirement_id"] == "REQ-010"
    assert sva_agent._checker_quality_issues(good, spec) == []


def test_sva_quality_rejects_tautological_assertions_and_constant_covers():
    spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-011", "checker_id": "a_req_011",
        "requirement": "When enable is high, done asserts on the next cycle.",
    }]}
    cases = {
        "constant assertion": """
a_req_011: assert property (@(posedge clk) 1'b1);
c_req_011: cover property (@(posedge clk) enable);
""",
        "constant consequent": """
a_req_011: assert property (@(posedge clk) enable |=> 1'b1);
c_req_011: cover property (@(posedge clk) enable);
""",
        "self comparison": """
a_req_011: assert property (@(posedge clk) enable |=> done == done);
c_req_011: cover property (@(posedge clk) enable);
""",
        "constant cover": """
a_req_011: assert property (@(posedge clk) enable |=> done);
c_req_011: cover property (@(posedge clk) 1'b1);
""",
    }

    for label, source in cases.items():
        issues = sva_agent._checker_quality_issues(source, spec)
        assert issues, label

    good = """
a_req_011: assert property (@(posedge clk) enable |=> done);
c_req_011: cover property (@(posedge clk) enable);
"""
    assert sva_agent._checker_quality_issues(good, spec) == []


def test_simulation_assertion_failure_maps_to_requirement():
    sva_spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-092", "checker_id": "a_req_092",
        "owner_module": "controller", "requirement": "Fault clear has priority.",
    }]}
    failures = execution_agent._assertion_failures(
        ["%Error: a_req_092: Assertion failed at time=42"], sva_spec
    )
    assert failures == [{
        "requirement_id": "REQ-092", "checker_id": "a_req_092",
        "owner_module": "controller", "requirement": "Fault clear has priority.",
        "failure_cycle_or_time": 42,
        "log_evidence": "%Error: a_req_092: Assertion failed at time=42",
        "repair_class": "behavioral_rtl",
    }]


def test_repeated_assertion_log_is_collapsed_to_first_actionable_failure():
    sva_spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-092", "checker_id": "a_req_092",
        "owner_module": "controller", "requirement": "Fault clear has priority.",
    }]}
    failures = execution_agent._assertion_failures([
        "a_req_092: Assertion failed at time=42",
        "a_req_092: Assertion failed at time=43",
    ], sva_spec)
    assert len(failures) == 1
    assert failures[0]["failure_cycle_or_time"] == 42


def test_assertion_failure_is_not_hidden_by_zero_simulator_return_code():
    failures = [{"requirement_id": "REQ-092", "checker_id": "a_req_092"}]
    assert execution_agent._test_passed(0, failures) is False
    assert execution_agent._test_passed(0, []) is True
    assert execution_agent._test_passed(None, []) is False


def test_sva_handoff_excludes_constraints_and_sta_obligations():
    spec = {"hierarchy": {"top_module": {
        "name": "pwm_controller",
        "ports": [{"name": "clk", "direction": "input"}],
        "behavior_rules": ["Meet a nominal 50 MHz timing target."],
    }}}
    assert sva_agent._behavioral_obligations(spec) == []


def test_sva_quality_rejects_checker_that_drops_explicit_comparison_relation():
    sva_spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-005",
        "checker_id": "a_req_005",
        "requirement": (
            "On reset counter_value becomes zero and pwm_out evaluates according to the "
            "comparison 0 < duty_cycle."
        ),
    }]}
    bad = "property p_req; @(posedge clk) (!reset_n) |=> (pwm_out == 1'b0); endproperty\n" \
          "a_req_005: assert property(p_req);"
    issues = sva_agent._checker_quality_issues(bad, sva_spec)
    assert any("does not preserve explicit relation" in item["issue"] for item in issues)


def test_requirement_assertion_actions_report_without_fatal_process_hang():
    source = '''
a_req_001: assert property(p_one);
a_req_002: assert property(p_two) else $error("bad");
'''
    normalized = sva_agent._make_assertion_failures_terminal(source)
    assert normalized.count("$display") == 2
    assert "ASSERTION_FAILURE a_req_001" in normalized
    assert "ASSERTION_FAILURE a_req_002" in normalized
    assert "$fatal" not in normalized
    assert "$error" not in normalized


def test_sva_property_names_are_distinct_from_tracked_cover_labels():
    source = """
property c_req_001;
  @(posedge clk) enable;
endproperty
c_req_001: cover property(c_req_001);
property a_req_002;
  @(posedge clk) enable |=> done;
endproperty
a_req_002: assert property(a_req_002);
"""

    normalized = sva_agent._rename_property_label_collisions(source)

    assert "property p_c_req_001;" in normalized
    assert "c_req_001: cover property(p_c_req_001);" in normalized
    assert "property p_a_req_002;" in normalized
    assert "a_req_002: assert property(p_a_req_002);" in normalized


def test_simulation_compile_failure_is_seed_invariant_and_short_circuited():
    stderr = [
        "%Error: assertions.sv:10: Unsupported in C: Block has the same name as PROPERTY",
        "%Error: Exiting due to 1 error(s)",
    ]
    assert execution_agent._compile_or_elaboration_failed(2, [], stderr, []) is True
    assert execution_agent._compile_or_elaboration_failed(
        2, ["Running tests"], stderr, []
    ) is False
    assert execution_agent._compile_or_elaboration_failed(
        2, [], stderr, [{"checker_id": "a_req_001"}]
    ) is False
    assert execution_agent._root_failure_class([{
        "pass": False, "compile_or_elaboration_failed": True,
    }]) == "compile_or_elaboration"


def test_summary_assertion_scan_does_not_count_generic_compiler_errors(tmp_path):
    reports = tmp_path / "reports" / "run_logs"
    reports.mkdir(parents=True)
    (reports / "compile.stderr.log").write_text(
        "%Error: assertions.sv: unsupported syntax\n", encoding="utf-8"
    )
    assert summary_agent._scan_assertion_failures(str(tmp_path / "reports")) == 0
    (reports / "simulation.stdout.log").write_text(
        "ASSERTION_FAILURE a_req_007\n", encoding="utf-8"
    )
    assert summary_agent._scan_assertion_failures(str(tmp_path / "reports")) == 1


def test_simulation_timeout_is_bounded_and_configurable():
    assert execution_agent._simulation_test_timeout_sec({}) == 180
    assert execution_agent._simulation_test_timeout_sec({"simulation_test_timeout_sec": 5}) == 30
    assert execution_agent._simulation_test_timeout_sec({"simulation_test_timeout_sec": 99999}) == 1800


def test_run_status_has_terminal_fallback_and_closure_defaults_to_three_attempts():
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    append_run = main_source.split("def append_log_run", 1)[1].split("# ==========================================================", 1)[0]
    assert 'supabase.table("runs").update(fallback)' in append_run
    closure_model = main_source.split("class DigitalVerifyClosureAppIn", 1)[1].split("class DigitalSmokeAppIn", 1)[0]
    assert "max_iterations: Optional[int] = 3" in closure_model


def test_nonvacuity_coverage_does_not_mistake_requirement_number_for_hit_count(tmp_path):
    reports = tmp_path / "reports"
    annotated = reports / "verilator_coverage_annotated"
    annotated.mkdir(parents=True)
    spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-092", "checker_id": "a_req_092", "cover_id": "c_req_092",
    }]}
    (annotated / "assertions.sv").write_text("c_req_092: cover property(trigger);\n", encoding="utf-8")
    result = execution_agent._nonvacuity_results(str(tmp_path), str(reports), spec)
    assert result[0]["status"] == "not_measured"
    (annotated / "assertions.sv").write_text("% 3 c_req_092: cover property(trigger);\n", encoding="utf-8")
    result = execution_agent._nonvacuity_results(str(tmp_path), str(reports), spec)
    assert result[0]["status"] == "hit"


def test_behavioral_repair_request_is_bounded_by_failure_fingerprint():
    debug_items = [{"assertion_failures": [{
        "requirement_id": "REQ-092", "checker_id": "a_req_092",
        "owner_module": "controller", "log_evidence": "failed at time=42",
    }]}]
    first = debug_agent._rtl_repair_request(debug_items, {})
    state = {"behavioral_repair_max_attempts": 2, "behavioral_repair_history": [
        {"fingerprint": first["fingerprint"]}, {"fingerprint": first["fingerprint"]},
    ]}
    blocked = debug_agent._rtl_repair_request(debug_items, state)
    assert blocked["status"] == "blocked_nonconvergent"
    assert blocked["attempt"] == 3


def test_structured_assertion_failure_triggers_repair_when_optional_debug_is_disabled(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    state = {
        "workflow_id": "closure", "workflow_dir": str(tmp_path / "closure"),
        "enable_failure_debug": False,
        "failure_triage": {"failures": [{
            "testcase": "smoke_test", "seed": 1, "stdout_tail": [], "stderr_tail": [],
            "assertion_failures": [{
                "requirement_id": "REQ-092", "checker_id": "a_req_092",
                "owner_module": "controller", "log_evidence": "a_req_092 failed",
            }],
        }]},
    }
    debug_agent.run_agent(state)
    assert state["rtl_repair_request"]["required"] is True
    assert state["rtl_repair_request"]["status"] == "ready_for_targeted_rtl_repair"
    assert state["failure_debug"]["summary"] == "structured_assertion_failures_auto_debugged"


def test_failure_fingerprint_ignores_cycle_specific_log_text():
    base = {"requirement_id": "REQ-092", "checker_id": "a_req_092", "owner_module": "controller"}
    first = debug_agent._rtl_repair_request(
        [{"assertion_failures": [{**base, "log_evidence": "failed at time=42"}]}], {}
    )
    second = debug_agent._rtl_repair_request(
        [{"assertion_failures": [{**base, "log_evidence": "failed at time=99"}]}], {}
    )
    assert first["fingerprint"] == second["fingerprint"]


def test_failure_triage_prefers_latest_closure_iteration_summary(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    workflow_dir = tmp_path / "closure"
    reports = workflow_dir / "vv" / "tb" / "reports"
    reports.mkdir(parents=True)
    latest = reports / "simulation_execution_summary.json"
    latest.write_text(json.dumps({"results": [{
        "testcase": "new_failure", "seed": 7, "pass": False, "rc": 1,
        "assertion_failures": [{"requirement_id": "REQ-200", "checker_id": "a_req_200"}],
    }]}), encoding="utf-8")
    state = {
        "workflow_id": "closure", "workflow_dir": str(workflow_dir),
        "source_verify_workflow_dir": str(tmp_path / "parent"),
        "simulation_execution_summary_json": str(latest),
        "source_simulation_execution_summary": {"results": [{
            "testcase": "old_failure", "seed": 1, "pass": False, "rc": 1,
        }]},
    }
    triage_agent.run_agent(state)
    assert state["failure_triage"]["failures"][0]["testcase"] == "new_failure"
    assert state["failure_triage"]["failures"][0]["assertion_failures"][0]["checker_id"] == "a_req_200"


def test_compile_failure_is_not_routed_to_behavioral_rtl_repair(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    workflow_dir = tmp_path / "closure"
    reports = workflow_dir / "vv" / "tb" / "reports"
    reports.mkdir(parents=True)
    latest = reports / "simulation_execution_summary.json"
    latest.write_text(json.dumps({
        "root_failure_class": "compile_or_elaboration",
        "results": [{
            "testcase": "smoke_test", "seed": 1, "pass": False, "rc": 2,
            "assertion_failures": [],
        }],
    }), encoding="utf-8")
    state = {
        "workflow_id": "closure", "workflow_dir": str(workflow_dir),
        "simulation_execution_summary_json": str(latest),
    }
    triage_agent.run_agent(state)
    debug_agent.run_agent(state)
    assert state["failure_triage"]["root_failure_class"] == "compile_or_elaboration"
    assert state["failure_triage"]["failures"][0]["classification"] == (
        "verification_collateral_compile_or_elaboration_failure"
    )
    assert state["rtl_repair_request"]["required"] is False


def test_closure_judge_stops_on_verification_infrastructure_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(iteration_judge_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    workflow_dir = tmp_path / "workflow"
    reports = workflow_dir / "vv" / "tb" / "reports"
    reports.mkdir(parents=True)
    summary_path = reports / "simulation_summary_coverage.json"
    summary_path.write_text(json.dumps({
        "simulation": {"total": 1, "pass": 0, "fail": 1},
    }), encoding="utf-8")
    state = {
        "workflow_id": "closure-child", "workflow_dir": str(workflow_dir),
        "simulation_summary_coverage_json": str(summary_path),
        "behavioral_assertion_failures": [{"checker_id": "a_req_092"}],
        "verification_quality_gate": {"passed": False, "root_failure_class": "compile_or_elaboration"},
    }
    iteration_judge_agent.run_agent(state)
    judgement = state["closure_iteration_judgement"]
    assert judgement["stop_reason"] == "verification_infrastructure_failure"
    assert judgement["continue_recommended"] is False


def test_later_closure_iteration_preserves_validated_repaired_rtl(tmp_path):
    repaired = tmp_path / "controller_repaired.sv"
    repaired.write_text("module controller_repaired; endmodule\n", encoding="utf-8")
    state = {
        "closure_iteration_index": 2,
        "rtl_files": [str(repaired)],
        "behavioral_rtl_repair": {
            "status": "candidate_validated_pending_focused_verification",
        },
        # These would normally force a Supabase import if preservation failed.
        "rtl_source_mode": "from_arch2rtl",
        "source_arch2rtl_workflow_id": "parent",
    }
    verification_handoff_agent.run_agent(state)
    assert state["rtl_files"] == [str(repaired)]
    assert state["status"].startswith("Preserved validated behavioral RTL repair")


def test_behavioral_rtl_repair_validates_candidate_before_verification_resume(tmp_path, monkeypatch):
    rtl = tmp_path / "controller.v"
    rtl.write_text("module controller(input clk); endmodule\n", encoding="utf-8")
    monkeypatch.setattr(behavioral_repair_agent, "_complete_rtl_text", lambda *args, **kwargs:
        "---BEGIN controller.v---\nmodule controller(input clk); endmodule\n---END controller.v---")
    monkeypatch.setattr(behavioral_repair_agent, "_validate_and_materialize_rtl", lambda **kwargs: {
        "ok": True, "artifact_list": [str(rtl)], "compile_passed": True,
        "lint_passed": True, "static_spec2rtl_passed": True,
    })
    monkeypatch.setattr(behavioral_repair_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    state = {
        "workflow_id": "repair-child", "workflow_dir": str(tmp_path / "workflow"),
        "rtl_files": [str(rtl)], "digital_spec": {"top_module": {"name": "controller"}},
        "rtl_repair_request": {
            "required": True, "status": "ready_for_targeted_rtl_repair", "attempt": 1,
            "fingerprint": "abc", "failures": [{"requirement_id": "REQ-092"}],
        },
    }
    behavioral_repair_agent.run_agent(state)
    assert state["behavioral_rtl_repair"]["status"] == "candidate_validated_pending_focused_verification"
    assert state["behavioral_rtl_repair"]["requirements"] == ["REQ-092"]


def test_behavioral_repair_context_is_scoped_to_owning_module(tmp_path):
    owner = tmp_path / "watchdog.sv"
    unrelated = tmp_path / "packetizer.sv"
    owner.write_text("module watchdog; endmodule\n", encoding="utf-8")
    unrelated.write_text("module packetizer; endmodule\n", encoding="utf-8")
    selected = behavioral_repair_agent._repair_context_files(
        [str(owner), str(unrelated)],
        {"failures": [{"owner_module": "watchdog"}]},
    )
    assert selected == [str(owner)]


def test_closure_judge_continues_bounded_behavioral_repair_without_coverage_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(iteration_judge_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    workflow_dir = tmp_path / "workflow"
    reports = workflow_dir / "vv" / "tb" / "reports"
    reports.mkdir(parents=True)
    summary_path = reports / "simulation_summary_coverage.json"
    summary_path.write_text(json.dumps({
        "coverage": {"functional_coverage_pct": 50},
        "simulation": {"total": 1, "pass": 0, "fail": 1},
    }), encoding="utf-8")
    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(workflow_dir),
        "closure_iteration_index": 1,
        "simulation_summary_coverage_json": str(summary_path),
        "behavioral_assertion_failures": [{"checker_id": "a_req_092"}],
        "behavioral_repair_history": [{"fingerprint": "abc", "attempt": 1}],
        "behavioral_repair_max_attempts": 3,
        "verification_quality_gate": {"passed": False},
    }
    iteration_judge_agent.run_agent(state)
    judgement = state["closure_iteration_judgement"]
    assert judgement["stop_reason"] == "behavioral_repair_retry"
    assert judgement["continue_recommended"] is True


def test_closure_registry_orders_repair_and_sva_before_testbench_generation():
    registry_path = Path(__file__).resolve().parents[1] / "registry" / "workflows.yaml"
    workflows = json.loads(registry_path.read_text(encoding="utf-8"))["workflows"]
    closure = next(item for item in workflows if item.get("name") == "Digital_Verify_Closure_Loop")
    agents = closure["agents"]
    assert agents.index("Digital Verification Handoff Ingest Agent") < agents.index("Digital Behavioral RTL Repair Agent")
    assert agents.index("Digital Behavioral RTL Repair Agent") < agents.index("Digital Assertions (SVA) Agent")
    assert agents.index("Digital Assertions (SVA) Agent") < agents.index("Digital Testbench Generator Agent")
    migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations" /
                 "phase_20260920_behavioral_rtl_verification_repair.sql").read_text(encoding="utf-8")
    digital_verify = migration.split("'Digital_Verify'", 1)[1].split("'Digital_Verify_Closure_Loop'", 1)[0]
    assert digital_verify.index("Digital Assertions (SVA) Agent") < digital_verify.index("Digital Testbench Generator Agent")


def test_verify_closure_agents_generate_plan_from_parent_verify_artifacts(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    monkeypatch.chdir(tmp_path)

    source_id = "verify-parent"
    source_reports = tmp_path / "backend" / "workflows" / source_id / "vv" / "tb" / "reports"
    run_logs = source_reports / "run_logs"
    run_logs.mkdir(parents=True)
    (source_reports / "simulation_summary_coverage.json").write_text(
        json.dumps(
            {
                "simulation": {"total": 2, "pass": 1, "fail": 1},
                "coverage": {
                    "functional_coverage_pct": 75.0,
                    "functional": {"coverage_pct": 75.0},
                    "code": {"line_coverage_pct": 70.0, "branch_coverage_pct": 50.0},
                },
            }
        ),
        encoding="utf-8",
    )
    (source_reports / "simulation_execution_summary.json").write_text(
        json.dumps(
            {
                "total": 2,
                "pass": 1,
                "fail": 1,
                "results": [
                    {"testcase": "smoke_test", "seed": 1, "pass": True, "rc": 0},
                    {"testcase": "constrained_random_sanity", "seed": 2, "pass": False, "rc": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    (source_reports / "functional_coverage_summary.json").write_text(
        json.dumps(
            {
                "outputs": {
                    "irq": {"hit_bins": 1, "total_bins": 2, "seen_values": [0]},
                }
            }
        ),
        encoding="utf-8",
    )
    (run_logs / "constrained_random_sanity__seed_2.stderr.log").write_text(
        "Assertion failed at pwm_controller.sv:88\n",
        encoding="utf-8",
    )

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_verify_workflow_id": source_id,
        "coverage_targets": "100% functional, 90% line, 80% branch",
        "seed_count": 4,
    }

    ingest_agent.run_agent(state)
    gap_agent.run_agent(state)
    triage_agent.run_agent(state)
    recommendation_agent.run_agent(state)

    plan_path = Path(state["workflow_dir"]) / "verify_closure" / "verify_closure_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["verdict"] == "debug_failures_first"
    assert plan["coverage_gap_count"] >= 1
    assert plan["functional_gap_count"] == 1
    assert plan["functional_gaps"][0]["coverage_point"] == "outputs.irq"
    assert plan["functional_gaps"][0]["missing_bins"] == ["nonzero"]
    assert plan["failure_count"] == 1
    assert any(item["id"] == "rerun_failed_seeds_with_waveform" for item in plan["recommended_actions"])


def test_system_sim_closure_updates_system_specific_seed_keys(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    monkeypatch.chdir(tmp_path)

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_system_sim_workflow_id": "system-parent",
        "source_simulation_manifest": {
            "top_module": "temp_monitor_soc_sim",
            "default_tests": ["system_smoke_test", "integrated_input_sanity", "register_access_directed", "output_activation_sweep"],
        },
        "closure_added_coverage_points": [
            {"id": "COV_ITER001_001", "source_gap_type": "functional_bin_gap", "coverage_point": "outputs.alert_irq"}
        ],
        "seed_budget": 4,
        "random_vs_directed": "both",
    }

    testcase_seed_agent.run_agent(state)

    assert state["system_sim_testcases"] == ["register_access_directed"]
    assert state["system_sim_seeds"] == [1, 2, 3, 4]
    assert state["simulation_seeds"] == [1, 2, 3, 4]


def test_verify_closure_ingest_exposes_materialized_rtl_to_digital_rerun(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    monkeypatch.chdir(tmp_path)

    source_id = "verify-parent"
    source_root = tmp_path / "backend" / "workflows" / source_id
    source_rtl = source_root / "verification" / "handoff" / "rtl"
    source_tb = source_root / "vv" / "tb"
    source_rtl.mkdir(parents=True)
    source_tb.mkdir(parents=True)
    (source_rtl / "pwm_controller.v").write_text("module pwm_controller(input clk); endmodule\n", encoding="utf-8")
    (source_tb / "simulation_manifest.json").write_text(
        json.dumps(
            {
                "top_module": "pwm_controller",
                "rtl_files": [
                    f"backend/workflows/{source_id}/verification/handoff/rtl/pwm_controller.v"
                ],
                "default_tests": ["smoke_test", "constrained_random_sanity"],
            }
        ),
        encoding="utf-8",
    )
    (source_tb / "tb_contract.json").write_text(
        json.dumps(
            {
                "top_module": "pwm_controller",
                "rtl_files": [
                    f"backend/workflows/{source_id}/verification/handoff/rtl/pwm_controller.v"
                ],
            }
        ),
        encoding="utf-8",
    )

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_verify_workflow_id": source_id,
    }

    ingest_agent.run_agent(state)

    assert [Path(path).name for path in state["rtl_files"]] == ["pwm_controller.v"]
    assert state["rtl_inputs"] == state["rtl_files"]
    assert state["source_rtl_files"] == state["rtl_files"]
    assert state["digital"]["rtl_files"] == state["rtl_files"]
    assert Path(state["rtl_files"][0]).is_file()


def test_verify_closure_rerun_planner_keeps_vv_manifest_as_digital_verify(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_verify_workflow_id": "verify-parent",
        "source_simulation_manifest": {
            "type": "vv_simulation_manifest",
            "top_module": "pwm_controller",
        },
        "source_verification_source_handoff": {
            "source_workflow_id": "arch2rtl-parent",
        },
    }

    rerun_planner_agent.run_agent(state)

    assert state["closure_rerun_manifest"]["closure_context"] == "digital_verify"
    assert state["closure_rerun_manifest"]["source_arch2rtl_workflow_id"] == "arch2rtl-parent"
    assert state["rtl_source_mode"] == "from_arch2rtl"
    assert state["from_workflow_id"] == "arch2rtl-parent"

def test_verify_closure_rerun_planner_accepts_inline_fpga_handoff(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)

    state = {
        "workflow_id": "fpga-current",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "fpga-current"),
        "rtl_source_mode": "paste",
        "upstream_workflows": {"requirements": "requirements-parent"},
        "verification_source_handoff": {
            "type": "fpga_verification_source_handoff",
            "source_workflow_id": "fpga-current",
            "rtl_source_kind": "fpga_current_workflow_rtl",
        },
    }

    rerun_planner_agent.run_agent(state)

    manifest = state["closure_rerun_manifest"]
    assert manifest["closure_context"] == "fpga_inline_verify"
    assert manifest["source_fpga_workflow_id"] == "fpga-current"
    assert manifest["source_arch2rtl_workflow_id"] is None
    assert state["rtl_source_mode"] == "paste"
    assert state["source_fpga_workflow_id"] == "fpga-current"
    assert state["upstream_workflows"] == {
        "requirements": "requirements-parent",
        "fpga": "fpga-current",
    }
    assert "from_workflow_id" not in state
    assert (
        tmp_path
        / "backend"
        / "workflows"
        / "fpga-current"
        / "verify_closure"
        / "iteration_001"
        / "rerun_manifest.json"
    ).is_file()
def test_sva_quality_rejects_checker_that_drops_conditional_exception():
    requirement = (
        "When reset_n is low, state is cleared. After reset is released, pwm_out is low "
        "until the compare condition counter_value < duty_cycle becomes true."
    )
    spec = {"behavioral_obligations": [{
        "requirement_id": "REQ-017", "checker_id": "a_req_017", "requirement": requirement,
    }]}
    bad = """
property p_req_017;
  @(posedge clk) !reset_n |=> ((counter_value == 8'h00) && (pwm_out == 1'b0));
endproperty
a_req_017: assert property (p_req_017);
c_req_017: cover property (@(posedge clk) !reset_n);
"""
    good = """
property p_req_017;
  @(posedge clk) !reset_n |=> ((counter_value == 8'h00) &&
    (pwm_out == ((counter_value < duty_cycle) ? 1'b1 : 1'b0)));
endproperty
a_req_017: assert property (p_req_017);
c_req_017: cover property (@(posedge clk) !reset_n);
"""
    issues = sva_agent._checker_quality_issues(bad, spec)
    assert any("conditional exception" in item["issue"] for item in issues)
    assert not any("conditional exception" in item["issue"] for item in sva_agent._checker_quality_issues(good, spec))


def test_sva_quality_rejects_invented_output_stability_and_counter_transition():
    spec = {"behavioral_obligations": [
        {"requirement_id": "REQ-001", "checker_id": "a_req_001",
         "requirement": "Maintain an 8-bit counter for PWM phase generation."},
        {"requirement_id": "REQ-004", "checker_id": "a_req_004",
         "requirement": "Provide the current counter value on the counter_value output."},
    ]}
    sva = """
property p1; @(posedge clk) 1'b1 |=> counter_value == $past(counter_value) + 1; endproperty
a_req_001: assert property(p1);
property p4; @(posedge clk) 1'b1 |-> counter_value == $past(counter_value); endproperty
a_req_004: assert property(p4);
"""
    issues = sva_agent._checker_quality_issues(sva, spec)
    assert any("invents counter transition" in item["issue"] for item in issues)
    assert any("invents temporal stability" in item["issue"] for item in issues)


def test_sva_quality_rejects_dropped_until_gate_and_combinational_reset_output():
    spec = {"behavioral_obligations": [
        {"requirement_id": "REQ-005", "checker_id": "a_req_005",
         "requirement": "Reset all sequential state to zero when reset_n is low."},
        {"requirement_id": "REQ-008", "checker_id": "a_req_008",
         "requirement": "pwm_out is high whenever counter_value < duty_cycle and low otherwise."},
        {"requirement_id": "REQ-014", "checker_id": "a_req_014",
         "requirement": (
             "When reset_n is low, the counter is cleared and pwm_out is driven low; "
             "upon release, the counter remains at zero until enable is asserted."
         )},
    ]}
    sva = """
property p5; @(posedge clk) !reset_n |=> counter_value == 0 && pwm_out == 0; endproperty
a_req_005: assert property(p5);
property p8; @(posedge clk) pwm_out == (counter_value < duty_cycle); endproperty
a_req_008: assert property(p8);
property p14; @(posedge clk) !reset_n |=> counter_value == 0 && pwm_out == 0; endproperty
a_req_014: assert property(p14);
"""
    issues = sva_agent._checker_quality_issues(sva, spec)
    assert any("combinational output pwm_out" in item["issue"] for item in issues)
    assert any("until-condition enable" in item["issue"] for item in issues)
