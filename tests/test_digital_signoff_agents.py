import os


os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_logic_equivalence_agent import _yosys_script
from agents.digital.digital_scan_atpg_agent import _adapter_log_has_execution_error, _metrics_show_real_atpg_result
from agents.digital.digital_tapeout_agent import _tapeout_failure_reasons, _xor_difference_count


def test_atpg_zero_pattern_metrics_are_not_success():
    assert not _metrics_show_real_atpg_result(
        pattern_count=0,
        stuck_at_coverage_pct=None,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )


def test_atpg_positive_patterns_or_coverage_are_success():
    assert _metrics_show_real_atpg_result(
        pattern_count=1,
        stuck_at_coverage_pct=None,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )
    assert _metrics_show_real_atpg_result(
        pattern_count=0,
        stuck_at_coverage_pct=84.5,
        faults_detected=None,
        faults_undetected=None,
        faults_aborted=None,
    )


def test_atpg_adapter_log_execution_errors_are_failures():
    assert _adapter_log_has_execution_error("/opt/chiploop-tools/run_atalanta_atpg.sh: line 1: No such file or directory")
    assert not _adapter_log_has_execution_error("Atalanta completed with 12 test patterns")


def test_lec_script_uses_configurable_induction_depth(monkeypatch):
    monkeypatch.setenv("CHIPLOOP_LEC_INDUCT_DEPTH", "96")

    script = _yosys_script(["rtl.sv"], "gate.v", "top", ["cells.v"])

    assert "equiv_induct -undef -seq 96" in script


def test_tapeout_status_is_signoff_gated():
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="Total XOR differences: 1",
        drc_status="failed",
        lvs_status="ok",
        klayout_gds="/tmp/top.klayout.gds",
        magic_gds=None,
    )

    assert "drc_not_clean" in reasons
    assert "xor_differences_found" in reasons


def test_tapeout_requires_gds_output():
    reasons = _tapeout_failure_reasons(
        rc=0,
        log="Total XOR differences: 0",
        drc_status="ok",
        lvs_status="ok",
        klayout_gds=None,
        magic_gds=None,
    )

    assert reasons == ["gds_not_produced"]


def test_tapeout_xor_difference_parser():
    assert _xor_difference_count("Total XOR differences: 0") == 0
    assert _xor_difference_count("One or more deferred errors encountered: 1 XOR differences found.") == 1
