# backend/agents/analog/analog_netlist_agent.py
import os, json, requests
from typing import Dict, Any, List
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _include_lines_for_tech(workflow_dir: str, technology: str, pdk_dir: str | None, pdk_files: List[str]) -> List[str]:
    lines = [f"* Technology: {technology}"]
    if pdk_dir and pdk_files:
        for rel in pdk_files:
            path = os.path.join(workflow_dir, rel)
            lines.append(f".include {path}")
    else:
        lines.append("* Using generic device models (no PDK includes)")
    return lines


def _fallback_netlist(spec: Dict[str, Any], workflow_dir: str) -> str:
    tech = spec.get("technology", "generic")
    vdd = spec.get("vdd", "1.8V")
    temp = spec.get("temperature", "25C")
    signals = spec.get("signals", {}) or {}
    vout = signals.get("Vout", "out")
    vin_def = signals.get("Vin", "sine(0 1 1k)")
    analysis = spec.get("analysis", {}) or {}
    components = spec.get("components", []) or []
    pdk_files = spec.get("pdk_files", [])
    pdk_dir = os.path.join(workflow_dir, "pdk")

    lines = [f"* {spec.get('title','Analog_Circuit')} — fallback netlist"]
    lines += _include_lines_for_tech(workflow_dir, tech, pdk_dir if pdk_files else None, pdk_files)
    lines.append(f".options TEMP={temp.replace('C','')}")

    # rails + input
    lines.append(f"VDD VDD 0 {vdd}")
    lines.append(f"VIN in 0 {vin_def}")

    def two(con):
        # map common pairs
        for a,b in (("pos","neg"),("p","n"),("node1","node2")):
            if a in con and b in con: return con[a], con[b]
        vs = list(con.values()); 
        return (vs[0] if vs else "in", vs[1] if len(vs)>1 else vout)

    for c in components:
        ctype = (c.get("type") or "").lower()
        name  = c.get("name") or "XU"
        val   = c.get("value") or ""
        mdl   = c.get("model") or ""
        con   = c.get("connections", {}) or {}

        if ctype in ("resistor","r"):
            n1,n2 = two(con); lines.append(f"{name} {n1} {n2} {val or '1k'}")
        elif ctype in ("capacitor","c"):
            n1,n2 = two(con); lines.append(f"{name} {n1} {n2} {val or '1u'}")
        elif ctype in ("inductor","l"):
            n1,n2 = two(con); lines.append(f"{name} {n1} {n2} {val or '1m'}")
        elif ctype in ("voltage_source","vsource","vsrc","vs"):
            n1,n2 = two(con); lines.append(f"{name} {n1} {n2} {val or 'dc 1'}")
        elif ctype in ("current_source","isource","isrc","is"):
            n1,n2 = two(con); lines.append(f"{name} {n1} {n2} {val or 'dc 1m'}")
        elif ctype in ("diode","d"):
            a,k = two(con); lines.append(f"{name} {a} {k} {mdl or 'DDEFAULT'}")
        elif ctype in ("transistor","bjt","npn","pnp","q"):
            cnode = con.get("c") or con.get("collector") or vout
            bnode = con.get("b") or con.get("base") or "in"
            enode = con.get("e") or con.get("emitter") or "0"
            lines.append(f"{name} {cnode} {bnode} {enode} {mdl or 'NPN'}")
        elif ctype in ("mosfet","mos","nmos","pmos","m"):
            d = con.get("d") or con.get("drain") or vout
            g = con.get("g") or con.get("gate") or "in"
            s = con.get("s") or con.get("source") or "0"
            b = con.get("b") or con.get("bulk") or s
            lines.append(f"{name} {d} {g} {s} {b} {mdl or ('NMOS' if 'n' in ctype else 'PMOS')}")
        elif ctype in ("opamp","op-amp","op_amp"):
            inp = con.get("inp","in"); inn = con.get("inn","fb"); out = con.get("out", vout)
            lines.append(f"E{name} {out} 0 {inp} {inn} 1e6")
            # annotate power pins if present
            vp = con.get("vp"); vm = con.get("vm")
            if vp or vm: lines.append(f"* {name} supplies: +={vp or 'VDD'}, -={vm or '0'}")
        else:
            # unknown component -> subckt instance
            node_list = " ".join(con.values()) if isinstance(con, dict) else ""
            lines.append(f"X{name} {node_list} {mdl or 'GENERIC'}")

    # ensure Vout not floating
    if all(vout not in ln for ln in lines if ln and ln[0] in "RCL"):
        lines.append(f"R_LOAD {vout} 0 10k")

    atype = str(analysis.get("type","AC")).upper()
    if atype == "AC":
        pts = analysis.get("points","10"); start = analysis.get("start","1"); stop = analysis.get("stop","1e6")
        lines.append(f".ac dec {pts} {start} {stop}")
        lines.append(f".print ac vdb({vout})")
    elif atype in ("TRAN","TRANSIENT"):
        step = analysis.get("step","10u"); stop = analysis.get("stop","10m")
        lines.append(f".tran {step} {stop}")
        lines.append(f".print tran v({vout})")
    else:
        lines.append(".ac dec 10 1 1e6"); lines.append(f".print ac vdb({vout})")

    lines.append(".end")
    return "\n".join(lines) + "\n"


def _llm_netlist(spec: Dict[str, Any]) -> str:
    prompt = f"""
You are a SPICE expert. Convert the following analog spec JSON into a valid ngspice netlist (.cir).

JSON:
{json.dumps(spec, indent=2)}

Requirements:
- Add .include lines for each file listed in "pdk_files" (paths are relative to the workflow directory).
- Add .options TEMP=<temperature without 'C'> if "temperature" exists.
- Create supply VDD source with value from "vdd" to node VDD.
- Add VIN source from signals.Vin on node "in" to 0.
- Use signals.Vout as the observable output node (default "out" if missing).
- Model op-amps with a VCVS: E<name> out 0 inp inn 1e6 (if no model provided).
- Finish with an analysis based on "analysis" (AC/TRAN) and one .print line.
- No markdown. Return the netlist text only.
"""
    if USE_LOCAL_OLLAMA:
        r = requests.post(OLLAMA_URL, json={"model":"llama3","prompt":prompt}, timeout=600)
        return r.text.strip()
    comp = client_portkey.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"user","content":prompt}]
    )
    return comp.choices[0].message.content.strip()


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    print("\n🧠 Running Analog Netlist Agent...")
    wf_id = state.get("workflow_id", "analog_default")
    wf_dir = state.get("workflow_dir", f"backend/workflows/{wf_id}")
    os.makedirs(wf_dir, exist_ok=True)

    spec = state.get("analog_spec") or {}
    spec_file = state.get("spec_file") or os.path.join(wf_dir, "analog_spec.json")
    if not spec and os.path.exists(spec_file):
        spec = json.load(open(spec_file, "r", encoding="utf-8"))

    if not spec:
        # ultra-safe fallback spec
        spec = {
            "title":"RC_Fallback_Filter","technology":"generic","vdd":"1.8V","temperature":"25C",
            "components":[
                {"type":"resistor","name":"R1","value":"1k","connections":{"pos":"in","neg":"out"}},
                {"type":"capacitor","name":"C1","value":"1u","connections":{"pos":"out","neg":"0"}}
            ],
            "signals":{"Vin":"sine(0 1 1k)","Vout":"out"},
            "analysis":{"type":"AC","start":"1","stop":"1e6","points":"10"},
            "pdk_files":[]
        }

    llm_error = None
    netlist = ""
    try:
        print(f"🌐 Using {'Ollama' if USE_LOCAL_OLLAMA else 'Portkey/OpenAI'} to generate netlist...")
        netlist = _llm_netlist(spec)
        assert ".end" in netlist.lower()
    except Exception as e:
        llm_error = str(e)
        print(f"⚠️ LLM failed, building fallback netlist: {llm_error}")
        netlist = _fallback_netlist(spec, wf_dir)

    # Ensure tech/PDK includes present in either path
    if "pdk_files" in spec and spec["pdk_files"]:
        includes = _include_lines_for_tech(wf_dir, spec.get("technology","generic"),
                                           os.path.join(wf_dir,"pdk"), spec["pdk_files"])
        # prepend includes after first line/comment
        nl = netlist.splitlines()
        insert_at = 1 if nl else 0
        netlist = "\n".join(nl[:insert_at] + includes + nl[insert_at:])

    net_path = os.path.join(wf_dir, "analog_netlist.cir")
    with open(net_path, "w", encoding="utf-8") as f:
        f.write(netlist)

    log_path = os.path.join(wf_dir, "analog_netlist_agent.log")
    with open(log_path, "w", encoding="utf-8") as log:
        if llm_error: log.write(f"LLM Error: {llm_error}\n\n")
        log.write("Spec:\n"); log.write(json.dumps(spec, indent=2)); log.write("\n\nNetlist:\n"); log.write(netlist)

    state.update({
        "status": "✅ Analog Netlist Generated",
        "netlist_file": net_path,
        "artifact": net_path,
        "artifact_log": log_path,
        "workflow_dir": wf_dir
    })
    print(f"✅ Netlist saved: {net_path}")
    return state
