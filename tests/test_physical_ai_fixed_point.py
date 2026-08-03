import csv

from physical_ai.fixed_point import analyze_fixed_point
from physical_ai.pmsm_equations import simulate_pmsm


def test_fixed_point_analysis_emits_zero_overflow_vectors_and_contract(tmp_path):
    simulation = simulate_pmsm({}, tmp_path)
    result = analyze_fixed_point(simulation["timeseries"], {}, tmp_path)
    assert result["analysis"]["passed"] is True
    assert result["analysis"]["total_overflow_count"] == 0
    assert result["rtl_contract"]["ports"]["speed_reference_rpm"]["word_bits"] == 16
    assert result["rtl_contract"]["ports"]["dc_bus_voltage_v"]["q_format"] == "Q6.9"
    assert result["rtl_contract"]["overflow"] == "saturate_and_raise_fault"
    with open(result["files"]["fixed_point_vectors"], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(simulation["timeseries"])
    assert "iq_a_raw" in rows[0]


def test_word_width_must_be_supported(tmp_path):
    simulation = simulate_pmsm({}, tmp_path)
    try:
        analyze_fixed_point(simulation["timeseries"], {"fixed_point_word_bits": 12}, tmp_path)
    except ValueError as exc:
        assert "16, 24, or 32" in str(exc)
    else:
        raise AssertionError("unsupported word width should fail")
