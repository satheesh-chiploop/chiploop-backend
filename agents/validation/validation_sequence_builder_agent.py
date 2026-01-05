import json, datetime
from utils.artifact_utils import save_text_artifact_and_record

def _pick_instrument(bench_setup: dict, inst_type: str):
    for inst in bench_setup.get("instruments", []):
        if inst.get("instrument_type") == inst_type:
            return inst
    return None

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id")
    bench = state.get("bench_setup") or {}
    plan = state.get("scoped_test_plan") or state.get("test_plan") or {}


    if not workflow_id or not bench or not plan:
        state["status"] = "❌ Missing workflow_id / bench_setup / test_plan"
        return state

    psu = _pick_instrument(bench, "psu")
    dmm = _pick_instrument(bench, "dmm")
    scope = _pick_instrument(bench, "scope")

    sequence = {
        "workflow_id": workflow_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "instruments": {
            "psu": psu,
            "dmm": dmm,
            "scope": scope
        },
        "tests": []
    }

    for t in plan.get("tests", []):
        steps = []
        # Always: IDN
        for inst_key, inst in [("psu", psu), ("dmm", dmm), ("scope", scope)]:
            if inst and inst_key in (t.get("required_instruments") or []):
                steps.append({
                    "type": "scpi",
                    "instrument": inst_key,
                    "resource": inst["resource_string"],
                    "cmd": "*IDN?",
                    "expect": "string"
                })

        # Example “power-up” patterns for Keysight PSU class (E3631x/E36312A)
        if psu and "psu" in (t.get("required_instruments") or []):
            steps += [
                {"type":"scpi","instrument":"psu","resource":psu["resource_string"],"cmd":"OUTP OFF"},
                {"type":"scpi","instrument":"psu","resource":psu["resource_string"],"cmd":"VOLT 1.800"},
                {"type":"scpi","instrument":"psu","resource":psu["resource_string"],"cmd":"CURR 0.500"},
                {"type":"scpi","instrument":"psu","resource":psu["resource_string"],"cmd":"OUTP ON"},
            ]

        # Example DMM read (34461A class)
        if dmm and "dmm" in (t.get("required_instruments") or []):
            steps += [
                {"type":"scpi","instrument":"dmm","resource":dmm["resource_string"],"cmd":"CONF:VOLT:DC AUTO"},
                {"type":"scpi","instrument":"dmm","resource":dmm["resource_string"],"cmd":"READ?","capture_as":"VOUT"}
            ]

        # Example scope capture (DSOX1xxx class — simplified)
        if scope and "scope" in (t.get("required_instruments") or []):
            steps += [
                {"type":"scpi","instrument":"scope","resource":scope["resource_string"],"cmd":":TIM:SCAL 1e-3"},
                {"type":"scpi","instrument":"scope","resource":scope["resource_string"],"cmd":":DIG"},
                {"type":"scpi","instrument":"scope","resource":scope["resource_string"],"cmd":":MEAS:VPP? CHAN1","capture_as":"VPP_CH1"}
            ]

        sequence["tests"].append({
            "name": t.get("name"),
            "tags": t.get("tags", []),
            "steps": steps
        })
    # ✅ FIX: use correct artifact_utils signature
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Sequence Builder Agent",
        subdir="validation",
        filename="test_sequence.json",
        content=json.dumps(sequence, indent=2),
    )

    

    state["test_sequence"] = sequence
    state["status"] = "✅ SCPI sequence generated"
    return state
