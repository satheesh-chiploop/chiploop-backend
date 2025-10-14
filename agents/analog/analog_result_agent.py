# backend/agents/analog/analog_result_agent.py
import os, json, math
from typing import Dict, Any, List, Tuple
from datetime import datetime
from portkey_ai import Portkey
from openai import OpenAI

# plotting is optional (fallback to text summary if not available)
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _load_csv(csv_path: str) -> Tuple[List[float], List[float]]:
    xs, ys = [], []
    if not csv_path or not os.path.exists(csv_path): return xs, ys
    with open(csv_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0: continue  # header
            try:
                x_str, y_str = line.strip().split(",")
                xs.append(float(x_str)); ys.append(float(y_str))
            except Exception:
                continue
    return xs, ys


def _summary_text(xs: List[float], ys: List[float]) -> str:
    if not xs or not ys: return "No numeric results parsed."
    try:
        # simple heuristics: AC magnitude peak, or final value for TRAN
        max_y = max(ys); max_x = xs[ys.index(max_y)]
        return f"Max output value ~ {max_y:.3f} at x={max_x:.3g}."
    except Exception:
        return "Result parsed, summary unavailable."


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analog Result Agent
    - Consumes sim CSV/log and spec
    - Creates a simple plot (if matplotlib available)
    - Uses LLM to write a concise Markdown report
    """
    print("\n📊 Running Analog Result Agent...")

    wf_id = state.get("workflow_id", "analog_default")
    wf_dir = state.get("workflow_dir", f"backend/workflows/{wf_id}")
    os.makedirs(wf_dir, exist_ok=True)

    csv_path = state.get("sim_csv")
    log_path = state.get("sim_log")
    spec = state.get("analog_spec")
    spec_file = state.get("spec_file") or os.path.join(wf_dir, "analog_spec.json")
    if not spec and os.path.exists(spec_file):
        spec = json.load(open(spec_file, "r", encoding="utf-8"))

    xs, ys = _load_csv(csv_path) if csv_path else ([], [])
    plot_path = None

    if HAS_MPL and xs and ys:
        try:
            fig = plt.figure(figsize=(6,4), dpi=120)
            # guess AC vs TRAN (frequency or time)
            if max(xs) > 100 and min(xs) >= 1:
                plt.semilogx(xs, ys)
                plt.xlabel("Frequency (Hz)")
                plt.ylabel("Output (dB or magnitude)")
                plt.title("Analog Simulation Result (AC)")
            else:
                plt.plot(xs, ys)
                plt.xlabel("Time (s)")
                plt.ylabel("Output (V)")
                plt.title("Analog Simulation Result (Transient)")
            plot_path = os.path.join(wf_dir, "analog_result.png")
            plt.tight_layout(); plt.savefig(plot_path); plt.close(fig)
        except Exception:
            plot_path = None

    # LLM analysis (short, focused)
    spec_snippet = json.dumps(spec, indent=2)[:2000] if spec else "{}"
    datum = _summary_text(xs, ys)

    analysis_prompt = f"""
You are an analog verification assistant.
Given the circuit spec and a short numeric summary from simulation, write a concise Markdown report:

Spec (JSON snippet):
{spec_snippet}

Numeric summary:
{datum}

Guidelines:
- Keep it under ~200 words.
- Explain what the result indicates (e.g., cutoff, gain, or trend).
- If the spec includes AC analysis, mention frequency behavior; if TRAN, mention time behavior.
- Provide 1-2 actionable suggestions (e.g., change R/C to shift cutoff).
Return Markdown only.
"""

    report_md = ""
    try:
        comp = client_portkey.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        report_md = comp.choices[0].message.content.strip()
    except Exception as e:
        report_md = f"### Result Summary\n\n{datum}\n\n*(LLM analysis unavailable: {e})*"

    report_path = os.path.join(wf_dir, "analog_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    state.update({
        "status": "✅ Analog Result Generated",
        "result_plot": plot_path,
        "result_report": report_path,
        "artifact": plot_path or report_path or log_path,
        "artifact_log": log_path,
        "workflow_dir": wf_dir
    })
    print(f"✅ Result report: {report_path}" + (f", Plot: {plot_path}" if plot_path else ""))
    return state
