import datetime
import json
from html import escape
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Product Dashboard Agent"
OUTPUT_SUBDIR = "system/product/app"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    return save_text_artifact_and_record(workflow_id, AGENT_NAME, subdir, filename, content)


def _html(model: Dict[str, Any]) -> str:
    name = escape(str(model.get("product_name") or "Device Control Dashboard"))
    model_json = json.dumps(model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{name}</title>
  <style>
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:#05070d; color:#e5edf7; }}
    main {{ max-width:1120px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    .sub {{ color:#9aa8bd; margin-bottom:22px; }}
    .grid {{ display:grid; grid-template-columns: 360px 1fr; gap:18px; }}
    .panel {{ border:1px solid #1f2a44; border-radius:12px; background:#080d18; padding:18px; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input[type=range] {{ width:100%; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:22px; font-weight:800; margin-top:6px; }}
    canvas {{ width:100%; height:260px; background:#030611; border:1px solid #1f2a44; border-radius:12px; }}
    pre {{ white-space:pre-wrap; max-height:220px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed product interface generated from validated RTL, firmware, software, and validation collateral.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
      <label><input id="enable" type="checkbox" checked /> Enable PWM</label>
      <label>Duty cycle: <span id="dutyLabel">55</span>%</label>
      <input id="duty" type="range" min="0" max="100" value="55" />
      <label>Period</label>
      <input id="period" type="range" min="16" max="255" value="100" />
      <div style="display:flex;gap:10px;margin-top:18px">
        <button id="apply">Apply</button>
        <button class="secondary" id="scenario">Run thermal scenario</button>
      </div>
      <h3>Lineage</h3>
      <pre id="lineage"></pre>
    </section>
    <section class="panel">
      <h2>Live Device State</h2>
      <canvas id="wave" width="760" height="260"></canvas>
      <div class="metrics">
        <div class="metric"><div>Enable</div><div id="mEnable">on</div></div>
        <div class="metric"><div>Duty</div><div id="mDuty">55%</div></div>
        <div class="metric"><div>Counter</div><div id="mCounter">0</div></div>
        <div class="metric"><div>PWM Out</div><div id="mOut">0</div></div>
      </div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
const state = {{ enable:true, duty:55, period:100, counter:0, out:0, history:[] }};
const duty = document.getElementById('duty');
const period = document.getElementById('period');
const enable = document.getElementById('enable');
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
function apply() {{ state.enable = enable.checked; state.duty = Number(duty.value); state.period = Number(period.value); document.getElementById('dutyLabel').textContent = state.duty; }}
function tick() {{ apply(); state.counter = state.enable ? (state.counter + 1) % Math.max(state.period,1) : 0; state.out = state.enable && state.counter < (state.duty/100)*state.period ? 1 : 0; state.history.push(state.out); if (state.history.length > 160) state.history.shift(); render(); }}
function render() {{
 document.getElementById('mEnable').textContent = state.enable ? 'on' : 'off';
 document.getElementById('mDuty').textContent = state.duty + '%';
 document.getElementById('mCounter').textContent = state.counter;
 document.getElementById('mOut').textContent = state.out;
 const c=document.getElementById('wave'), x=c.getContext('2d'); x.clearRect(0,0,c.width,c.height); x.strokeStyle='#22d3ee'; x.lineWidth=3; x.beginPath();
 state.history.forEach((v,i)=>{{ const px=i*(c.width/160); const py=v?70:190; if(i===0)x.moveTo(px,py); else x.lineTo(px,py); }});
 x.stroke();
}}
document.getElementById('apply').onclick = apply;
document.getElementById('scenario').onclick = () => {{ let vals=[0,25,55,90], i=0; const t=setInterval(()=>{{ duty.value=vals[i++%vals.length]; apply(); if(i>8) clearInterval(t); }}, 900); }};
setInterval(tick, 80);
</script>
</body>
</html>"""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    model = state.get("system_product_capability_model") if isinstance(state.get("system_product_capability_model"), dict) else {}
    html = _html(model)
    manifest = {
        "type": "system_product_dashboard_manifest",
        "generated_at": _now(),
        "app_type": "simulator_backed_web_dashboard",
        "entrypoint": f"{OUTPUT_SUBDIR}/index.html",
        "model_path": state.get("system_product_capability_model_path"),
        "hardware_replacement_note": "Replace the simulator adapter with UART/JTAG/Ethernet/board transport to control real hardware later.",
    }
    _record_text(workflow_id, "index.html", html)
    _record_text(workflow_id, "system_product_dashboard_manifest.json", json.dumps(manifest, indent=2))
    state["system_product_dashboard_manifest"] = manifest
    state["system_product_dashboard_entrypoint"] = manifest["entrypoint"]
    state["status"] = "System product dashboard generated"
    return state
