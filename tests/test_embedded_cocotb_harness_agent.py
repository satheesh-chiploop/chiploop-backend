import os

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
