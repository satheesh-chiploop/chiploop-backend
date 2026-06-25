import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_rtl_linting_agent as agent


def test_verilator_warning_codes_extracts_structural_warnings():
    stderr = """
%Warning-UNDRIVEN: top.sv:23:9: Signal is not driven
%Warning-WIDTHEXPAND: model.v:31:33: Operator ADD expects 64 bits
%Warning-MULTIDRIVEN: top.sv:24:16: Signal has multiple driving blocks
"""

    codes = agent._verilator_warning_codes(stderr)
    blocking = [
        code for code in codes
        if code in agent.SYNTHESIS_BLOCKING_VERILATOR_WARNINGS
    ]

    assert codes == ["UNDRIVEN", "WIDTHEXPAND", "MULTIDRIVEN"]
    assert blocking == ["UNDRIVEN", "MULTIDRIVEN"]
