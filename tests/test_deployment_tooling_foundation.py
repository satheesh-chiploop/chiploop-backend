from deployment_modes import active_deployment_mode, deployment_summary
from artifact_policy import artifact_may_sync
from license_policy import license_summary
from tooling.adapters import list_adapters, resolve_adapter
from tooling.profiles import profile_diagnostics, resolve_tool
from tooling.runner import TOOL_ALIASES, run_command


def test_default_deployment_mode_is_hosted_saas(monkeypatch):
    monkeypatch.delenv("CHIPLOOP_DEPLOYMENT_MODE", raising=False)
    active = active_deployment_mode()
    assert active.mode == "hosted_saas"
    assert active.frontend_owner == "chiploop"
    assert active.backend_owner == "chiploop"


def test_deployment_summary_lists_hybrid_and_private_modes():
    summary = deployment_summary()
    modes = {item["mode"] for item in summary["available_modes"]}
    assert {"hosted_saas", "hybrid_runner", "hybrid_private_data", "private", "customer_cloud"}.issubset(modes)


def test_tool_profile_diagnostics_include_commercial_placeholders():
    diag = profile_diagnostics({})
    assert "synopsys_dc" in diag["tools"]
    assert "cadence_genus" in diag["tools"]
    assert "xcelium" in diag["tools"]
    assert "vcs" in diag["tools"]
    assert "python" in diag["runtime"]


def test_tool_adapter_registry_contains_open_source_and_private_templates():
    adapter_ids = {item["adapter_id"] for item in list_adapters()}
    assert "verilator.lint" in adapter_ids
    assert "gem5.sim" in adapter_ids
    assert "synopsys.dc.synthesis" in adapter_ids
    assert "cadence.xcelium.sim" in adapter_ids
    assert resolve_adapter("verilator.lint").tool == "verilator"


def test_runner_aliases_include_customer_eda_tools():
    assert TOOL_ALIASES["dc_shell"] == "synopsys_dc"
    assert TOOL_ALIASES["genus"] == "cadence_genus"
    assert TOOL_ALIASES["xrun"] == "xcelium"
    assert TOOL_ALIASES["vcs"] == "vcs"


def test_strict_tool_profile_disables_path_fallback():
    state = {
        "tool_profile": {
            "profile_id": "strict-test",
            "strict_tool_profile": True,
            "tools": {},
            "runtime": {},
        }
    }
    assert resolve_tool("definitely-not-configured", state) is None
    result = run_command(state, "test", ["definitely-not-configured", "--version"])
    assert result.status == "tool_unavailable"


def test_artifact_policy_blocks_private_sync():
    assert artifact_may_sync("rtl.sv", "full_sync") is True
    assert artifact_may_sync("rtl.sv", "private_storage") is False
    assert artifact_may_sync("report.md", "summary_only") is True
    assert artifact_may_sync("wave.vcd", "summary_only") is False


def test_license_summary_defaults_by_deployment_mode(monkeypatch):
    monkeypatch.setenv("CHIPLOOP_DEPLOYMENT_MODE", "private")
    monkeypatch.delenv("CHIPLOOP_LICENSE_MODE", raising=False)
    monkeypatch.delenv("CHIPLOOP_LICENSE_KEY", raising=False)
    summary = license_summary()
    assert summary["mode"] == "private_enterprise"
    assert summary["license_key_configured"] is False
    assert summary["third_party_tool_licenses"] == "customer_managed"
