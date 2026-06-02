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
    :root {{ color-scheme:dark; --bg:#05070d; --panel:#080d18; --line:#1f2a44; --muted:#9aa8bd; --cyan:#22d3ee; --green:#10b981; --amber:#f59e0b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:#e5edf7; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    h2 {{ margin:0 0 12px; }}
    h3 {{ margin:18px 0 8px; }}
    .sub {{ color:var(--muted); margin-bottom:22px; max-width:900px; line-height:1.5; }}
    .grid {{ display:grid; grid-template-columns: 390px minmax(0, 1fr); gap:18px; align-items:start; }}
    .panel {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); padding:18px; min-width:0; }}
    .explain {{ display:grid; gap:10px; margin-bottom:14px; }}
    .note {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; color:#b8c4d8; line-height:1.45; }}
    .note b {{ color:#e5edf7; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input[type=range] {{ width:100%; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button:disabled {{ opacity:.65; cursor:default; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; min-width:0; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:22px; font-weight:800; margin-top:6px; overflow-wrap:anywhere; }}
    .scenario {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .step {{ border:1px solid #22304c; border-radius:10px; padding:10px; background:#050914; color:#93a4bd; }}
    .step.active {{ border-color:var(--cyan); color:#e5edf7; box-shadow:0 0 0 1px rgba(34,211,238,.25) inset; }}
    .status {{ color:#b8c4d8; min-height:22px; margin-top:12px; }}
    canvas {{ width:100%; height:260px; background:#030611; border:1px solid #1f2a44; border-radius:12px; }}
    pre {{ white-space:pre-wrap; max-height:220px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns:1fr; }} .metrics,.scenario {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed product interface generated from validated RTL, firmware, software, and validation collateral. This page models how the PWM controller would drive a fan before the same software is connected to a board or silicon transport.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
      <div class="explain">
        <div class="note"><b>Counter</b> is the internal PWM time position. It counts from 0 to period - 1, then wraps. The PWM output is high while the counter is below the duty threshold.</div>
        <div class="note"><b>Period</b> is the number of controller clock ticks in one PWM cycle. With the simulated 1 MHz clock, frequency is 1,000,000 / period. Period 100 gives a 10 kHz PWM signal.</div>
      </div>
      <label><input id="enable" type="checkbox" checked /> Enable PWM</label>
      <label>Duty cycle: <span id="dutyLabel">55</span>%</label>
      <input id="duty" type="range" min="0" max="100" value="55" />
      <label>Period: <span id="periodLabel">100</span> clock ticks</label>
      <input id="period" type="range" min="16" max="255" value="100" />
      <div style="display:flex;gap:10px;margin-top:18px">
        <button id="apply">Apply</button>
        <button class="secondary" id="scenario">Run thermal scenario</button>
      </div>
      <div class="status" id="scenarioStatus">Manual control mode.</div>
      <div class="scenario">
        <div class="step" id="step0">25 C<br />fan off</div>
        <div class="step" id="step1">45 C<br />quiet</div>
        <div class="step" id="step2">60 C<br />nominal</div>
        <div class="step" id="step3">80 C<br />cooldown</div>
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
        <div class="metric"><div>Period</div><div id="mPeriod">100 ticks</div></div>
        <div class="metric"><div>PWM Frequency</div><div id="mFreq">10.00 kHz</div></div>
        <div class="metric"><div>Counter</div><div id="mCounter">0</div></div>
        <div class="metric"><div>PWM Out</div><div id="mOut">0</div></div>
        <div class="metric"><div>High Threshold</div><div id="mThreshold">55 ticks</div></div>
        <div class="metric"><div>Measured High</div><div id="mMeasured">0%</div></div>
      </div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
const simulatedClockHz = 1000000;
const state = {{ enable:true, duty:55, period:100, counter:0, out:0, history:[], temperature:60, scenarioRunning:false }};
const duty = document.getElementById('duty');
const period = document.getElementById('period');
const enable = document.getElementById('enable');
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
function formatFreq(periodTicks) {{
  const hz = simulatedClockHz / Math.max(periodTicks, 1);
  return hz >= 1000 ? (hz / 1000).toFixed(2) + ' kHz' : hz.toFixed(0) + ' Hz';
}}
function clearScenarioMarks() {{
  for (let i=0; i<4; i++) document.getElementById('step' + i).classList.remove('active');
}}
function apply() {{
  state.enable = enable.checked;
  state.duty = Number(duty.value);
  state.period = Number(period.value);
  document.getElementById('dutyLabel').textContent = state.duty;
  document.getElementById('periodLabel').textContent = state.period;
}}
function tick() {{
  apply();
  const threshold = Math.round((state.duty / 100) * state.period);
  state.counter = state.enable ? (state.counter + 1) % Math.max(state.period,1) : 0;
  state.out = state.enable && state.counter < threshold ? 1 : 0;
  state.history.push(state.out);
  if (state.history.length > 160) state.history.shift();
  render();
}}
function render() {{
 document.getElementById('mEnable').textContent = state.enable ? 'on' : 'off';
 document.getElementById('mDuty').textContent = state.duty + '%';
 document.getElementById('mPeriod').textContent = state.period + ' ticks';
 document.getElementById('mFreq').textContent = formatFreq(state.period);
 document.getElementById('mCounter').textContent = state.counter;
 document.getElementById('mOut').textContent = state.out;
 document.getElementById('mThreshold').textContent = Math.round((state.duty / 100) * state.period) + ' ticks';
 const high = state.history.length ? Math.round((state.history.reduce((a,b)=>a+b,0) / state.history.length) * 100) : 0;
 document.getElementById('mMeasured').textContent = high + '%';
 const c=document.getElementById('wave'), x=c.getContext('2d'); x.clearRect(0,0,c.width,c.height); x.strokeStyle='#22d3ee'; x.lineWidth=3; x.beginPath();
 state.history.forEach((v,i)=>{{ const px=i*(c.width/160); const py=v?70:190; if(i===0)x.moveTo(px,py); else x.lineTo(px,py); }});
 x.stroke();
}}
function setPolicyStep(index, temp, nextDuty, nextPeriod, label) {{
  clearScenarioMarks();
  document.getElementById('step' + index).classList.add('active');
  state.temperature = temp;
  enable.checked = nextDuty > 0;
  duty.value = nextDuty;
  period.value = nextPeriod;
  state.counter = 0;
  state.history = [];
  apply();
  document.getElementById('scenarioStatus').textContent = temp + ' C thermal input -> ' + label + ', duty ' + nextDuty + '%, period ' + nextPeriod + ' ticks, PWM ' + formatFreq(nextPeriod) + '.';
  render();
}}
document.getElementById('apply').onclick = () => {{ clearScenarioMarks(); document.getElementById('scenarioStatus').textContent = 'Manual control mode.'; apply(); render(); }};
document.getElementById('scenario').onclick = () => {{
  if (state.scenarioRunning) return;
  state.scenarioRunning = true;
  document.getElementById('scenario').disabled = true;
  const steps = [
    [0, 25, 0, 160, 'idle fan-off policy'],
    [1, 45, 25, 128, 'quiet airflow policy'],
    [2, 60, 55, 100, 'nominal cooling policy'],
    [3, 80, 90, 64, 'thermal recovery policy']
  ];
  let i = 0;
  setPolicyStep(...steps[i++]);
  const timer = setInterval(() => {{
    if (i >= steps.length) {{
      clearInterval(timer);
      state.scenarioRunning = false;
      document.getElementById('scenario').disabled = false;
      document.getElementById('scenarioStatus').textContent += ' Scenario complete.';
      return;
    }}
    setPolicyStep(...steps[i++]);
  }}, 1400);
}};
setInterval(tick, 80);
</script>
</body>
</html>"""


def _uart_html(model: Dict[str, Any]) -> str:
    name = escape(str(model.get("product_name") or "UART Packet Engine Dashboard"))
    model_json = json.dumps(model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{name}</title>
  <style>
    :root {{ color-scheme:dark; --bg:#05070d; --panel:#080d18; --line:#1f2a44; --muted:#9aa8bd; --cyan:#22d3ee; --green:#10b981; --amber:#f59e0b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:#e5edf7; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    h2 {{ margin:0 0 12px; }}
    h3 {{ margin:18px 0 8px; }}
    .sub {{ color:var(--muted); margin-bottom:22px; max-width:900px; line-height:1.5; }}
    .grid {{ display:grid; grid-template-columns: 390px minmax(0,1fr); gap:18px; align-items:start; }}
    .panel {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); padding:18px; min-width:0; }}
    .explain {{ display:grid; gap:10px; margin-bottom:14px; }}
    .note {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; color:#b8c4d8; line-height:1.45; }}
    .note b {{ color:#e5edf7; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input, textarea {{ width:100%; box-sizing:border-box; border:1px solid #22304c; border-radius:10px; background:#030611; color:#e5edf7; padding:10px; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button:disabled {{ opacity:.65; cursor:default; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; min-width:0; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:20px; font-weight:800; margin-top:6px; word-break:break-word; }}
    .fifo {{ height:220px; display:flex; align-items:end; gap:18px; padding:18px; border:1px solid #1f2a44; border-radius:12px; background:#030611; }}
    .bar {{ width:90px; background:#0e7490; border-radius:8px 8px 0 0; transition:height .2s; }}
    .scenario {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .step {{ border:1px solid #22304c; border-radius:10px; padding:10px; background:#050914; color:#93a4bd; }}
    .step.active {{ border-color:var(--cyan); color:#e5edf7; box-shadow:0 0 0 1px rgba(34,211,238,.25) inset; }}
    .status {{ color:#b8c4d8; min-height:22px; margin-top:12px; }}
    pre {{ white-space:pre-wrap; max-height:220px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns:1fr; }} .metrics,.scenario {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed UART packet interface generated from validated RTL, firmware, software, and validation collateral. This page models a firmware-visible packet engine before the same driver is connected to a board or silicon transport.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
      <div class="explain">
        <div class="note"><b>TX FIFO</b> is the queue of bytes waiting to leave the UART. <b>RX FIFO</b> is the queue of bytes received by the engine. Loopback sends transmitted bytes back into RX so the software path can be exercised without external hardware.</div>
        <div class="note"><b>Baud divider</b> controls serial bit timing. In this simulator the displayed baud rate is 50 MHz / divider / 16, matching a common oversampling UART model.</div>
      </div>
      <label><input id="enable" type="checkbox" checked /> Enable engine</label>
      <label><input id="loopback" type="checkbox" checked /> Loopback mode</label>
      <label>Baud divider</label>
      <input id="baud" type="number" min="1" value="27" />
      <label>Packet length</label>
      <input id="plen" type="number" min="1" max="16" value="4" />
      <label>Packet payload</label>
      <textarea id="payload" rows="3">43 48 49 50</textarea>
      <div style="display:flex;gap:10px;margin-top:18px;flex-wrap:wrap">
        <button id="send">Send packet</button>
        <button class="secondary" id="error">Inject framing error</button>
        <button class="secondary" id="clear">Clear IRQ</button>
        <button class="secondary" id="scenario">Run packet scenario</button>
      </div>
      <div class="status" id="scenarioStatus">Manual packet mode.</div>
      <div class="scenario">
        <div class="step" id="ustep0">Boot<br />idle</div>
        <div class="step" id="ustep1">Loopback<br />packet</div>
        <div class="step" id="ustep2">Error<br />recovery</div>
      </div>
      <h3>Lineage</h3>
      <pre id="lineage"></pre>
    </section>
    <section class="panel">
      <h2>Live Packet Engine State</h2>
      <div class="fifo">
        <div><div id="txbar" class="bar" style="height:20px"></div><div>TX FIFO</div></div>
        <div><div id="rxbar" class="bar" style="height:20px;background:#10b981"></div><div>RX FIFO</div></div>
      </div>
      <div class="metrics">
        <div class="metric"><div>TX Level</div><div id="mTx">0</div></div>
        <div class="metric"><div>RX Level</div><div id="mRx">0</div></div>
        <div class="metric"><div>IRQ</div><div id="mIrq">0</div></div>
        <div class="metric"><div>IRQ Status</div><div id="mStatus">idle</div></div>
        <div class="metric"><div>Packets</div><div id="mPackets">0</div></div>
        <div class="metric"><div>Last RX</div><div id="mLast">--</div></div>
        <div class="metric"><div>Baud Div</div><div id="mBaud">27</div></div>
        <div class="metric"><div>Engine</div><div id="mEnable">on</div></div>
        <div class="metric"><div>Estimated Baud</div><div id="mRate">115.7 kbaud</div></div>
        <div class="metric"><div>Loopback</div><div id="mLoopback">on</div></div>
        <div class="metric"><div>Payload Bytes</div><div id="mPayload">4</div></div>
        <div class="metric"><div>Driver Action</div><div id="mAction">idle</div></div>
      </div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
const state = {{ tx:0, rx:0, irq:false, irqStatus:'idle', packets:0, lastRx:'--', busy:false, action:'idle', scenarioRunning:false }};
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
function clearScenarioMarks() {{
  for (let i=0; i<3; i++) document.getElementById('ustep' + i).classList.remove('active');
}}
function estimatedBaud() {{
  const div = Math.max(1, Number(document.getElementById('baud').value || 1));
  const baud = 50000000 / div / 16;
  return baud >= 1000 ? (baud / 1000).toFixed(1) + ' kbaud' : baud.toFixed(0) + ' baud';
}}
function render() {{
  document.getElementById('mTx').textContent = state.tx;
  document.getElementById('mRx').textContent = state.rx;
  document.getElementById('mIrq').textContent = state.irq ? '1' : '0';
  document.getElementById('mStatus').textContent = state.irqStatus;
  document.getElementById('mPackets').textContent = state.packets;
  document.getElementById('mLast').textContent = state.lastRx;
  document.getElementById('mBaud').textContent = document.getElementById('baud').value;
  document.getElementById('mEnable').textContent = document.getElementById('enable').checked ? 'on' : 'off';
  document.getElementById('mRate').textContent = estimatedBaud();
  document.getElementById('mLoopback').textContent = document.getElementById('loopback').checked ? 'on' : 'off';
  document.getElementById('mPayload').textContent = payloadBytes().length;
  document.getElementById('mAction').textContent = state.action;
  document.getElementById('txbar').style.height = Math.max(12, state.tx * 12) + 'px';
  document.getElementById('rxbar').style.height = Math.max(12, state.rx * 12) + 'px';
}}
function payloadBytes() {{
  return document.getElementById('payload').value.trim().split(/\\s+/).filter(Boolean).slice(0, 16);
}}
function sendPacket() {{
  if (!document.getElementById('enable').checked) return;
  if (state.busy) return;
  const bytes = payloadBytes();
  state.busy = true;
  state.tx = bytes.length;
  state.irqStatus = 'tx_active';
  state.irq = false;
  state.action = 'transmit';
  render();
  let idx = 0;
  const loopback = document.getElementById('loopback').checked;
  const timer = setInterval(() => {{
    state.tx = Math.max(0, state.tx - 1);
    if (loopback) {{ state.rx = Math.min(16, state.rx + 1); state.lastRx = bytes[idx] || '--'; }}
    idx += 1;
    if (state.tx === 0) {{
      state.packets += 1;
      state.irq = true;
      state.irqStatus = loopback ? 'tx_done|rx_done|packet_done' : 'tx_done|packet_done';
      state.action = loopback ? 'loopback complete' : 'transmit complete';
      state.busy = false;
      clearInterval(timer);
    }}
    render();
  }}, 350);
}}
document.getElementById('send').onclick = () => {{ clearScenarioMarks(); document.getElementById('scenarioStatus').textContent = 'Manual packet mode.'; sendPacket(); }};
document.getElementById('error').onclick = () => {{ clearScenarioMarks(); state.irq = true; state.irqStatus = 'framing_error'; state.action = 'error injected'; document.getElementById('scenarioStatus').textContent = 'Framing error injected. Clear IRQ to recover.'; render(); }};
document.getElementById('clear').onclick = () => {{ state.irq = false; state.irqStatus = 'idle'; state.rx = 0; state.action = 'irq cleared'; render(); }};
document.getElementById('scenario').onclick = () => {{
  if (state.scenarioRunning) return;
  state.scenarioRunning = true;
  document.getElementById('scenario').disabled = true;
  clearScenarioMarks();
  document.getElementById('ustep0').classList.add('active');
  document.getElementById('scenarioStatus').textContent = 'Boot step: engine enabled, FIFOs empty, IRQ clear.';
  document.getElementById('enable').checked = true;
  document.getElementById('loopback').checked = true;
  state.tx = 0; state.rx = 0; state.irq = false; state.irqStatus = 'idle'; state.action = 'boot preflight';
  render();
  setTimeout(() => {{
    clearScenarioMarks(); document.getElementById('ustep1').classList.add('active');
    document.getElementById('payload').value = '43 48 49 50 4c 4f 4f 50';
    document.getElementById('scenarioStatus').textContent = 'Loopback step: firmware writes CHIPLOOP packet and waits for packet_done IRQ.';
    sendPacket();
  }}, 1200);
  setTimeout(() => {{
    clearScenarioMarks(); document.getElementById('ustep2').classList.add('active');
    state.irq = true; state.irqStatus = 'framing_error'; state.action = 'error recovery'; render();
    document.getElementById('scenarioStatus').textContent = 'Error recovery step: framing error observed, then cleared by software.';
  }}, 5200);
  setTimeout(() => {{
    state.irq = false; state.irqStatus = 'idle'; state.rx = 0; state.action = 'scenario complete'; render();
    document.getElementById('scenarioStatus').textContent = 'Scenario complete: packet path and interrupt recovery both exercised.';
    document.getElementById('scenario').disabled = false;
    state.scenarioRunning = false;
  }}, 6800);
}};
document.getElementById('baud').oninput = render;
document.getElementById('payload').oninput = render;
render();
</script>
</body>
</html>"""


def _image_html(model: Dict[str, Any]) -> str:
    name = escape(str(model.get("product_name") or "Image DMA Pipeline Dashboard"))
    model_json = json.dumps(model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{name}</title>
  <style>
    :root {{ color-scheme:dark; --bg:#05070d; --panel:#080d18; --line:#1f2a44; --muted:#9aa8bd; --cyan:#22d3ee; --green:#10b981; --amber:#f59e0b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:var(--bg); color:#e5edf7; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    h2 {{ margin:0 0 12px; }}
    h3 {{ margin:18px 0 8px; }}
    .sub {{ color:var(--muted); margin-bottom:22px; max-width:900px; line-height:1.5; }}
    .grid {{ display:grid; grid-template-columns: 390px minmax(0,1fr); gap:18px; align-items:start; }}
    .panel {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); padding:18px; min-width:0; }}
    .explain {{ display:grid; gap:10px; margin-bottom:14px; }}
    .note {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; color:#b8c4d8; line-height:1.45; }}
    .note b {{ color:#e5edf7; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input, select {{ width:100%; box-sizing:border-box; border:1px solid #22304c; border-radius:10px; background:#030611; color:#e5edf7; padding:10px; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button:disabled {{ opacity:.65; cursor:default; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; min-width:0; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:20px; font-weight:800; margin-top:6px; word-break:break-word; }}
    .frames {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    canvas {{ width:100%; height:220px; image-rendering:pixelated; border:1px solid #1f2a44; border-radius:12px; background:#030611; }}
    .progress {{ height:14px; overflow:hidden; border-radius:999px; background:#1f2937; }}
    .progress div {{ height:100%; width:0%; background:#10b981; transition:width .2s; }}
    .scenario {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .step {{ border:1px solid #22304c; border-radius:10px; padding:10px; background:#050914; color:#93a4bd; }}
    .step.active {{ border-color:var(--cyan); color:#e5edf7; box-shadow:0 0 0 1px rgba(34,211,238,.25) inset; }}
    .status {{ color:#b8c4d8; min-height:22px; margin-top:12px; }}
    pre {{ white-space:pre-wrap; max-height:180px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 980px) {{ .grid,.frames {{ grid-template-columns:1fr; }} .metrics,.scenario {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed image-processing dashboard generated from validated RTL, firmware, software, and validation collateral. This page models the DMA and pixel pipeline path before the same control software is connected to a camera, memory subsystem, or accelerator board.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
      <div class="explain">
        <div class="note"><b>DMA progress</b> represents how many pixels have moved through the frame pipeline. A complete 64 x 64 frame is 4096 pixels in this generated demo.</div>
        <div class="note"><b>Filter mode</b> changes the processing kernel. The output frame and histogram update from the same generated software-facing controls used in the validation journey.</div>
      </div>
      <label>Filter mode</label>
      <select id="filter"><option value="bypass">Bypass</option><option value="blur">Blur</option><option value="edge">Edge detect</option><option value="threshold">Threshold</option></select>
      <label>Threshold: <span id="thresholdLabel">128</span></label>
      <input id="threshold" type="range" min="0" max="255" value="128" />
      <label>Brightness: <span id="brightLabel">0</span></label>
      <input id="brightness" type="range" min="-64" max="64" value="0" />
      <div style="display:flex;gap:10px;margin-top:18px;flex-wrap:wrap">
        <button id="start">Start frame</button>
        <button class="secondary" id="preset">Load edge preset</button>
        <button class="secondary" id="clear">Clear IRQ</button>
        <button class="secondary" id="scenario">Run vision scenario</button>
      </div>
      <div class="status" id="scenarioStatus">Manual frame mode.</div>
      <div class="scenario">
        <div class="step" id="istep0">Load<br />frame</div>
        <div class="step" id="istep1">Edge<br />detect</div>
        <div class="step" id="istep2">Threshold<br />classify</div>
      </div>
      <h3>Lineage</h3>
      <pre id="lineage"></pre>
    </section>
    <section class="panel">
      <h2>Frame Preview</h2>
      <div class="frames">
        <div><div style="margin-bottom:8px;color:#93a4bd">Input frame</div><canvas id="input" width="64" height="64"></canvas></div>
        <div><div style="margin-bottom:8px;color:#93a4bd">Output frame</div><canvas id="output" width="64" height="64"></canvas></div>
      </div>
      <div style="margin:16px 0 8px;color:#93a4bd">DMA progress</div>
      <div class="progress"><div id="progress"></div></div>
      <canvas id="hist" width="640" height="140" style="margin-top:14px;height:140px"></canvas>
      <div class="metrics">
        <div class="metric"><div>Status</div><div id="mStatus">idle</div></div>
        <div class="metric"><div>IRQ</div><div id="mIrq">0</div></div>
        <div class="metric"><div>Pixels</div><div id="mPixels">0</div></div>
        <div class="metric"><div>Histogram</div><div id="mHist">ready</div></div>
        <div class="metric"><div>Filter</div><div id="mFilter">bypass</div></div>
        <div class="metric"><div>Frame Count</div><div id="mFrames">0</div></div>
        <div class="metric"><div>Target FFs</div><div>~25k</div></div>
        <div class="metric"><div>Error</div><div id="mError">none</div></div>
        <div class="metric"><div>Frame Size</div><div>64 x 64</div></div>
        <div class="metric"><div>DMA Progress</div><div id="mProgress">0%</div></div>
        <div class="metric"><div>Output Mean</div><div id="mMean">0</div></div>
        <div class="metric"><div>Driver Action</div><div id="mAction">idle</div></div>
      </div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
const input = document.getElementById('input');
const output = document.getElementById('output');
const hist = document.getElementById('hist');
const ictx = input.getContext('2d');
const octx = output.getContext('2d');
const hctx = hist.getContext('2d');
const src = new Uint8ClampedArray(64*64);
let dst = new Uint8ClampedArray(64*64);
let frames = 0;
let irq = false;
let busy = false;
let action = 'idle';
function seed() {{
  for (let y=0;y<64;y++) for (let x=0;x<64;x++) src[y*64+x] = (x*4 + y*3 + ((x^y)&15)*7) & 255;
}}
function drawFrame(ctx, data) {{
  const img = ctx.createImageData(64,64);
  for (let i=0;i<data.length;i++) {{ img.data[i*4]=data[i]; img.data[i*4+1]=data[i]; img.data[i*4+2]=data[i]; img.data[i*4+3]=255; }}
  ctx.putImageData(img,0,0);
}}
function processPixel(x,y,mode,thr,bri) {{
  const v = src[y*64+x];
  let out = v + bri;
  if (mode === 'threshold') out = v >= thr ? 255 : 0;
  if (mode === 'edge') {{
    const l = src[y*64+Math.max(0,x-1)], r = src[y*64+Math.min(63,x+1)], u = src[Math.max(0,y-1)*64+x], d = src[Math.min(63,y+1)*64+x];
    out = Math.min(255, Math.abs(r-l) + Math.abs(d-u) + bri);
  }}
  if (mode === 'blur') {{
    let sum=0,c=0; for(let dy=-1;dy<=1;dy++) for(let dx=-1;dx<=1;dx++) {{ const xx=Math.min(63,Math.max(0,x+dx)), yy=Math.min(63,Math.max(0,y+dy)); sum += src[yy*64+xx]; c++; }}
    out = Math.round(sum/c) + bri;
  }}
  return Math.max(0, Math.min(255, out));
}}
function drawHistogram(data) {{
  const bins = new Array(32).fill(0);
  data.forEach(v => bins[Math.min(31, Math.floor(v/8))]++);
  hctx.clearRect(0,0,hist.width,hist.height); hctx.fillStyle='#22d3ee';
  const max = Math.max(...bins,1);
  bins.forEach((b,i) => {{ const h=(b/max)*(hist.height-18); hctx.fillRect(i*(hist.width/32)+2, hist.height-h, (hist.width/32)-4, h); }});
}}
function outputMean() {{
  if (!dst.length) return 0;
  let sum = 0;
  for (let i=0; i<dst.length; i++) sum += dst[i];
  return Math.round(sum / dst.length);
}}
function render(status, pixels) {{
  drawFrame(ictx, src); drawFrame(octx, dst); drawHistogram(dst);
  document.getElementById('mStatus').textContent = status;
  document.getElementById('mIrq').textContent = irq ? '1' : '0';
  document.getElementById('mPixels').textContent = pixels;
  document.getElementById('mFilter').textContent = document.getElementById('filter').value;
  document.getElementById('mFrames').textContent = frames;
  document.getElementById('mHist').textContent = pixels >= 4096 ? 'done' : 'updating';
  document.getElementById('thresholdLabel').textContent = document.getElementById('threshold').value;
  document.getElementById('brightLabel').textContent = document.getElementById('brightness').value;
  document.getElementById('mProgress').textContent = Math.round((pixels / 4096) * 100) + '%';
  document.getElementById('mMean').textContent = outputMean();
  document.getElementById('mAction').textContent = action;
}}
function clearScenarioMarks() {{
  for (let i=0; i<3; i++) document.getElementById('istep' + i).classList.remove('active');
}}
function startFrame(statusLabel='busy') {{
  if (busy) return;
  busy = true;
  const mode = document.getElementById('filter').value, thr = Number(document.getElementById('threshold').value), bri = Number(document.getElementById('brightness').value);
  dst = new Uint8ClampedArray(64*64); irq = false; let idx=0;
  action = 'dma ' + mode;
  const timer = setInterval(() => {{
    for (let n=0;n<96 && idx<4096;n++,idx++) {{ const x=idx%64, y=Math.floor(idx/64); dst[idx]=processPixel(x,y,mode,thr,bri); }}
    document.getElementById('progress').style.width = Math.round((idx/4096)*100) + '%';
    render(statusLabel, idx);
    if (idx>=4096) {{ clearInterval(timer); frames++; irq=true; busy=false; action = mode + ' complete'; render('frame_done', idx); }}
  }}, 40);
}}
document.getElementById('start').onclick = () => {{ clearScenarioMarks(); document.getElementById('scenarioStatus').textContent = 'Manual frame mode.'; startFrame('busy'); }};
document.getElementById('preset').onclick = () => {{ clearScenarioMarks(); document.getElementById('filter').value='edge'; document.getElementById('threshold').value='96'; action='edge preset'; render('preset_loaded',0); }};
document.getElementById('clear').onclick = () => {{ irq=false; action='irq cleared'; render('idle',0); document.getElementById('progress').style.width='0%'; }};
document.getElementById('scenario').onclick = () => {{
  if (busy) return;
  document.getElementById('scenario').disabled = true;
  clearScenarioMarks();
  document.getElementById('istep0').classList.add('active');
  document.getElementById('filter').value='bypass';
  document.getElementById('brightness').value='0';
  document.getElementById('threshold').value='128';
  document.getElementById('scenarioStatus').textContent = 'Load step: DMA moves one raw frame through bypass mode.';
  startFrame('load_frame');
  setTimeout(() => {{
    clearScenarioMarks(); document.getElementById('istep1').classList.add('active');
    document.getElementById('filter').value='edge';
    document.getElementById('brightness').value='12';
    document.getElementById('threshold').value='96';
    document.getElementById('scenarioStatus').textContent = 'Edge step: software selects edge-detect kernel and reruns the frame.';
    startFrame('edge_detect');
  }}, 2500);
  setTimeout(() => {{
    clearScenarioMarks(); document.getElementById('istep2').classList.add('active');
    document.getElementById('filter').value='threshold';
    document.getElementById('brightness').value='0';
    document.getElementById('threshold').value='150';
    document.getElementById('scenarioStatus').textContent = 'Threshold step: classify bright pixels and assert frame_done IRQ.';
    startFrame('threshold_classify');
  }}, 5000);
  setTimeout(() => {{
    document.getElementById('scenario').disabled = false;
    action = 'scenario complete';
    document.getElementById('scenarioStatus').textContent = 'Scenario complete: load, edge detect, threshold, DMA progress, histogram, and IRQ were exercised.';
    render('frame_done',4096);
  }}, 7600);
}};
document.getElementById('filter').onchange = () => render('configured',0);
document.getElementById('threshold').oninput = () => render('configured',0);
document.getElementById('brightness').oninput = () => render('configured',0);
seed(); dst = src.slice(); render('idle',0);
</script>
</body>
</html>"""


def _generic_html(model: Dict[str, Any]) -> str:
    name = escape(str(model.get("product_name") or "Generated Device Control Dashboard"))
    model_json = json.dumps(model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{name}</title>
  <style>
    :root {{ color-scheme:dark; --bg:#05070d; --panel:#080d18; --line:#1f2a44; --muted:#9aa8bd; --cyan:#22d3ee; --green:#10b981; --amber:#f59e0b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, Arial, sans-serif; background:var(--bg); color:#e5edf7; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    h2 {{ margin:0 0 12px; }}
    h3 {{ margin:18px 0 8px; }}
    .sub {{ color:var(--muted); margin-bottom:22px; max-width:920px; line-height:1.5; }}
    .grid {{ display:grid; grid-template-columns:390px minmax(0,1fr); gap:18px; align-items:start; }}
    .panel {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); padding:18px; min-width:0; }}
    .note {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; color:#b8c4d8; line-height:1.45; margin-bottom:10px; }}
    .note b {{ color:#e5edf7; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input, select {{ width:100%; box-sizing:border-box; border:1px solid #22304c; border-radius:10px; background:#030611; color:#e5edf7; padding:10px; }}
    input[type=range] {{ padding:0; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button.secondary {{ background:#1f2937; }}
    button:disabled {{ opacity:.65; cursor:default; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:14px; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; min-width:0; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:22px; font-weight:800; margin-top:6px; overflow-wrap:anywhere; }}
    .progress {{ height:14px; overflow:hidden; border-radius:999px; background:#1f2937; margin:12px 0 16px; }}
    .progress div {{ height:100%; width:0%; background:var(--green); transition:width .2s; }}
    .scenario {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .step {{ border:1px solid #22304c; border-radius:10px; padding:10px; background:#050914; color:#93a4bd; }}
    .step.active {{ border-color:var(--cyan); color:#e5edf7; box-shadow:0 0 0 1px rgba(34,211,238,.25) inset; }}
    .log {{ min-height:120px; border:1px solid #1f2a44; border-radius:12px; background:#030611; padding:12px; color:#b8c4d8; white-space:pre-wrap; }}
    pre {{ white-space:pre-wrap; max-height:200px; overflow:auto; color:#b8c4d8; }}
    @media (max-width:980px) {{ .grid {{ grid-template-columns:1fr; }} .metrics,.scenario {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub" id="summary">Simulator-backed product interface generated from validated RTL, firmware, software, and validation collateral.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
      <div id="controlExplain"></div>
      <label><input id="enable" type="checkbox" checked /> Enable</label>
      <label>Mode</label>
      <select id="mode"><option value="normal">Normal</option><option value="diagnostic">Diagnostic</option><option value="stress">Stress</option></select>
      <label>Level: <span id="levelLabel">50</span></label>
      <input id="level" type="range" min="0" max="100" value="50" />
      <div style="display:flex;gap:10px;margin-top:18px;flex-wrap:wrap">
        <button id="apply">Apply</button>
        <button class="secondary" id="start">Start operation</button>
        <button class="secondary" id="clear">Clear IRQ</button>
        <button class="secondary" id="scenario">Run product scenario</button>
      </div>
      <h3>Scenario</h3>
      <div id="scenarioStatus" class="note">Manual product mode.</div>
      <div id="scenarioSteps" class="scenario"></div>
      <h3>Lineage</h3>
      <pre id="lineage"></pre>
    </section>
    <section class="panel">
      <h2>Live Device State</h2>
      <div id="metricExplain"></div>
      <div style="margin-top:12px;color:#93a4bd">Operation progress</div>
      <div class="progress"><div id="progress"></div></div>
      <div class="metrics">
        <div class="metric"><div>Status</div><div id="mStatus">idle</div></div>
        <div class="metric"><div>Progress</div><div id="mProgress">0%</div></div>
        <div class="metric"><div>Result</div><div id="mResult">0</div></div>
        <div class="metric"><div>IRQ</div><div id="mIrq">0</div></div>
        <div class="metric"><div>Mode</div><div id="mMode">normal</div></div>
        <div class="metric"><div>Level</div><div id="mLevel">50</div></div>
        <div class="metric"><div>Capabilities</div><div id="mCaps">0</div></div>
        <div class="metric"><div>Transport</div><div>sim</div></div>
      </div>
      <h3>Activity Log</h3>
      <div id="log" class="log">Ready.</div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
const exp = model.product_experience || {{}};
const state = {{ status:'idle', progress:0, result:0, irq:false, busy:false }};
function entries(obj) {{ return obj && typeof obj === 'object' ? Object.entries(obj) : []; }}
function renderExplain(target, obj) {{
  const host = document.getElementById(target);
  host.innerHTML = '';
  entries(obj).slice(0, 5).forEach(([k,v]) => {{
    const div = document.createElement('div');
    div.className = 'note';
    div.innerHTML = '<b>' + String(k) + '</b><br />' + String(v);
    host.appendChild(div);
  }});
}}
function renderScenarioSteps() {{
  const host = document.getElementById('scenarioSteps');
  host.innerHTML = '';
  const steps = Array.isArray(exp.scenario_steps) && exp.scenario_steps.length ? exp.scenario_steps : [
    {{label:'Configure', description:'Apply product settings'}},
    {{label:'Execute', description:'Run one operation'}},
    {{label:'Observe', description:'Read status and result'}}
  ];
  steps.slice(0, 4).forEach((step, idx) => {{
    const div = document.createElement('div');
    div.className = 'step';
    div.id = 'gstep' + idx;
    div.innerHTML = String(step.label || ('Step ' + (idx + 1))) + '<br />' + String(step.description || '');
    host.appendChild(div);
  }});
}}
function setActiveStep(idx) {{
  document.querySelectorAll('.step').forEach(x => x.classList.remove('active'));
  const el = document.getElementById('gstep' + idx);
  if (el) el.classList.add('active');
}}
function appendLog(line) {{
  const log = document.getElementById('log');
  log.textContent = (log.textContent === 'Ready.' ? '' : log.textContent + '\\n') + line;
}}
function apply() {{
  document.getElementById('levelLabel').textContent = document.getElementById('level').value;
  render();
}}
function render() {{
  const mode = document.getElementById('mode').value;
  const level = Number(document.getElementById('level').value || 0);
  document.getElementById('mStatus').textContent = state.status;
  document.getElementById('mProgress').textContent = state.progress + '%';
  document.getElementById('mResult').textContent = state.result;
  document.getElementById('mIrq').textContent = state.irq ? '1' : '0';
  document.getElementById('mMode').textContent = mode;
  document.getElementById('mLevel').textContent = level;
  document.getElementById('mCaps').textContent = Array.isArray(model.capabilities) ? model.capabilities.length : 0;
  document.getElementById('progress').style.width = state.progress + '%';
}}
function startOperation(label='operation') {{
  if (state.busy || !document.getElementById('enable').checked) return;
  state.busy = true; state.status = 'busy'; state.progress = 0; state.irq = false; render();
  appendLog(label + ': started');
  const mode = document.getElementById('mode').value;
  const level = Number(document.getElementById('level').value || 0);
  const timer = setInterval(() => {{
    state.progress = Math.min(100, state.progress + 10);
    state.result = Math.round((level + state.progress) * (mode === 'stress' ? 3 : mode === 'diagnostic' ? 2 : 1));
    render();
    if (state.progress >= 100) {{
      clearInterval(timer);
      state.busy = false; state.status = 'done'; state.irq = true; render();
      appendLog(label + ': done, result=' + state.result + ', irq=1');
    }}
  }}, 180);
}}
document.getElementById('summary').textContent = exp.summary || document.getElementById('summary').textContent;
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
renderExplain('controlExplain', exp.control_explanations);
renderExplain('metricExplain', exp.metric_explanations);
renderScenarioSteps();
document.getElementById('scenario').textContent = exp.scenario_name || 'Run product scenario';
document.getElementById('apply').onclick = () => {{ apply(); appendLog('settings applied'); }};
document.getElementById('start').onclick = () => startOperation('manual operation');
document.getElementById('clear').onclick = () => {{ state.irq = false; state.status = 'idle'; state.progress = 0; appendLog('irq cleared'); render(); }};
document.getElementById('scenario').onclick = () => {{
  if (state.busy) return;
  document.getElementById('scenario').disabled = true;
  setActiveStep(0);
  document.getElementById('scenarioStatus').textContent = 'Configure step: applying mode and level through generated software API.';
  document.getElementById('mode').value = 'diagnostic';
  document.getElementById('level').value = '65';
  apply();
  appendLog('scenario: configure');
  setTimeout(() => {{
    setActiveStep(1);
    document.getElementById('scenarioStatus').textContent = 'Execute step: starting one simulator-backed product transaction.';
    startOperation('scenario operation');
  }}, 900);
  setTimeout(() => {{
    setActiveStep(2);
    document.getElementById('scenarioStatus').textContent = 'Observe step: status, result, and IRQ are visible to the application.';
    appendLog('scenario: observe status=' + state.status + ', result=' + state.result + ', irq=' + (state.irq ? '1' : '0'));
    document.getElementById('scenario').disabled = false;
  }}, 3400);
}};
document.getElementById('level').oninput = apply;
document.getElementById('mode').onchange = apply;
render();
</script>
</body>
</html>"""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    model = state.get("system_product_capability_model") if isinstance(state.get("system_product_capability_model"), dict) else {}
    if model.get("device_model") == "image_dma_pipeline":
        html = _image_html(model)
    elif model.get("device_model") == "uart_packet_engine":
        html = _uart_html(model)
    elif model.get("device_model") in {"generic_device_control", "smart_sensor_hub_mcu"}:
        html = _generic_html(model)
    else:
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
