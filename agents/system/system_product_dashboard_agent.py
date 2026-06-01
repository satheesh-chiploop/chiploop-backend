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
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:#05070d; color:#e5edf7; }}
    main {{ max-width:1120px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    .sub {{ color:#9aa8bd; margin-bottom:22px; }}
    .grid {{ display:grid; grid-template-columns: 380px 1fr; gap:18px; }}
    .panel {{ border:1px solid #1f2a44; border-radius:12px; background:#080d18; padding:18px; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input, textarea {{ width:100%; box-sizing:border-box; border:1px solid #22304c; border-radius:10px; background:#030611; color:#e5edf7; padding:10px; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:20px; font-weight:800; margin-top:6px; word-break:break-word; }}
    .fifo {{ height:220px; display:flex; align-items:end; gap:18px; padding:18px; border:1px solid #1f2a44; border-radius:12px; background:#030611; }}
    .bar {{ width:90px; background:#0e7490; border-radius:8px 8px 0 0; transition:height .2s; }}
    pre {{ white-space:pre-wrap; max-height:220px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed UART packet interface generated from validated RTL, firmware, software, and validation collateral.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
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
      </div>
    </section>
  </div>
</main>
<script>
const model = {model_json};
const state = {{ tx:0, rx:0, irq:false, irqStatus:'idle', packets:0, lastRx:'--' }};
document.getElementById('lineage').textContent = JSON.stringify(model.lineage || {{}}, null, 2);
function render() {{
  document.getElementById('mTx').textContent = state.tx;
  document.getElementById('mRx').textContent = state.rx;
  document.getElementById('mIrq').textContent = state.irq ? '1' : '0';
  document.getElementById('mStatus').textContent = state.irqStatus;
  document.getElementById('mPackets').textContent = state.packets;
  document.getElementById('mLast').textContent = state.lastRx;
  document.getElementById('mBaud').textContent = document.getElementById('baud').value;
  document.getElementById('mEnable').textContent = document.getElementById('enable').checked ? 'on' : 'off';
  document.getElementById('txbar').style.height = Math.max(12, state.tx * 12) + 'px';
  document.getElementById('rxbar').style.height = Math.max(12, state.rx * 12) + 'px';
}}
function payloadBytes() {{
  return document.getElementById('payload').value.trim().split(/\\s+/).filter(Boolean).slice(0, 16);
}}
document.getElementById('send').onclick = () => {{
  if (!document.getElementById('enable').checked) return;
  const bytes = payloadBytes();
  state.tx = bytes.length;
  state.irqStatus = 'tx_active';
  state.irq = false;
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
      clearInterval(timer);
    }}
    render();
  }}, 350);
}};
document.getElementById('error').onclick = () => {{ state.irq = true; state.irqStatus = 'framing_error'; render(); }};
document.getElementById('clear').onclick = () => {{ state.irq = false; state.irqStatus = 'idle'; state.rx = 0; render(); }};
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
    body {{ margin:0; font-family: Inter, Arial, sans-serif; background:#05070d; color:#e5edf7; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    h1 {{ margin:0 0 6px; color:#67e8f9; }}
    .sub {{ color:#9aa8bd; margin-bottom:22px; }}
    .grid {{ display:grid; grid-template-columns: 360px 1fr; gap:18px; }}
    .panel {{ border:1px solid #1f2a44; border-radius:12px; background:#080d18; padding:18px; }}
    label {{ display:block; color:#b8c4d8; margin:14px 0 8px; }}
    input, select {{ width:100%; box-sizing:border-box; border:1px solid #22304c; border-radius:10px; background:#030611; color:#e5edf7; padding:10px; }}
    button {{ border:0; border-radius:10px; padding:10px 14px; background:#0891b2; color:white; font-weight:700; cursor:pointer; }}
    button.secondary {{ background:#1f2937; }}
    .metric {{ border:1px solid #22304c; border-radius:10px; padding:12px; background:#050914; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:14px; }}
    .metric div:first-child {{ color:#93a4bd; font-size:12px; }}
    .metric div:last-child {{ font-size:20px; font-weight:800; margin-top:6px; word-break:break-word; }}
    .frames {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    canvas {{ width:100%; height:220px; image-rendering:pixelated; border:1px solid #1f2a44; border-radius:12px; background:#030611; }}
    .progress {{ height:14px; overflow:hidden; border-radius:999px; background:#1f2937; }}
    .progress div {{ height:100%; width:0%; background:#10b981; transition:width .2s; }}
    pre {{ white-space:pre-wrap; max-height:180px; overflow:auto; color:#b8c4d8; }}
    @media (max-width: 900px) {{ .grid,.frames {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} }}
  </style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <div class="sub">Simulator-backed image-processing dashboard generated from validated RTL, firmware, software, and validation collateral.</div>
  <div class="grid">
    <section class="panel">
      <h2>Controls</h2>
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
}}
document.getElementById('start').onclick = () => {{
  const mode = document.getElementById('filter').value, thr = Number(document.getElementById('threshold').value), bri = Number(document.getElementById('brightness').value);
  dst = new Uint8ClampedArray(64*64); irq = false; let idx=0;
  const timer = setInterval(() => {{
    for (let n=0;n<96 && idx<4096;n++,idx++) {{ const x=idx%64, y=Math.floor(idx/64); dst[idx]=processPixel(x,y,mode,thr,bri); }}
    document.getElementById('progress').style.width = Math.round((idx/4096)*100) + '%';
    render('busy', idx);
    if (idx>=4096) {{ clearInterval(timer); frames++; irq=true; render('frame_done', idx); }}
  }}, 40);
}};
document.getElementById('preset').onclick = () => {{ document.getElementById('filter').value='edge'; document.getElementById('threshold').value='96'; render('preset_loaded',0); }};
document.getElementById('clear').onclick = () => {{ irq=false; render('idle',0); document.getElementById('progress').style.width='0%'; }};
seed(); dst = src.slice(); render('idle',0);
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
