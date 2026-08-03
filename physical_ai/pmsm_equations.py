import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List


def _positive(payload: Dict[str, Any], key: str, default: float) -> float:
    value = float(payload.get(key, default))
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _line_svg(rows: List[Dict[str, float]], x_key: str, y_key: str, title: str, y_label: str) -> str:
    width, height, pad = 900, 420, 60
    xs, ys = [row[x_key] for row in rows], [row[y_key] for row in rows]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(0.0, min(ys)), max(ys)
    if y_max == y_min:
        y_max = y_min + 1.0
    points = " ".join(
        f"{pad + (x - x_min) / max(x_max - x_min, 1e-12) * (width - 2 * pad):.1f},"
        f"{height - pad - (y - y_min) / (y_max - y_min) * (height - 2 * pad):.1f}"
        for x, y in zip(xs, ys)
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#020617"/><text x="{width/2}" y="28" fill="#e2e8f0" text-anchor="middle" font-family="sans-serif" font-size="18">{title}</text>
<line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#64748b"/><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#64748b"/>
<polyline points="{points}" fill="none" stroke="#a78bfa" stroke-width="3"/>
<text x="{width/2}" y="{height-15}" fill="#94a3b8" text-anchor="middle" font-family="sans-serif">Time (s)</text><text x="18" y="{height/2}" fill="#94a3b8" text-anchor="middle" font-family="sans-serif" transform="rotate(-90 18 {height/2})">{y_label}</text>
<text x="{pad}" y="{height-pad+20}" fill="#94a3b8" font-family="sans-serif">{x_min:.3g}</text><text x="{width-pad}" y="{height-pad+20}" fill="#94a3b8" text-anchor="end" font-family="sans-serif">{x_max:.3g}</text>
<text x="{pad-8}" y="{pad}" fill="#94a3b8" text-anchor="end" font-family="sans-serif">{y_max:.3g}</text><text x="{pad-8}" y="{height-pad}" fill="#94a3b8" text-anchor="end" font-family="sans-serif">{y_min:.3g}</text></svg>'''


def _sweep_svg(rows: List[Dict[str, Any]], speeds: List[float], loads: List[float]) -> str:
    width, height, left, top, cell = 900, 500, 120, 65, 120
    cells = []
    lookup = {(row["target_speed_rpm"], row["load_torque_nm"]): row for row in rows}
    for yi, load in enumerate(reversed(loads)):
        cells.append(f'<text x="{left-12}" y="{top+yi*cell+cell/2+5}" fill="#cbd5e1" text-anchor="end" font-family="sans-serif">{load:g}</text>')
        for xi, speed in enumerate(speeds):
            row = lookup[(speed, load)]
            color = "#166534" if row["feasible"] else "#991b1b"
            x, y = left + xi * cell, top + yi * cell
            cells.append(f'<rect x="{x}" y="{y}" width="{cell-4}" height="{cell-4}" rx="7" fill="{color}"/>')
            cells.append(f'<text x="{x+(cell-4)/2}" y="{y+cell/2}" fill="white" text-anchor="middle" font-family="sans-serif" font-size="13">{row["final_speed_rpm"]:.0f} rpm</text>')
    for xi, speed in enumerate(speeds):
        cells.append(f'<text x="{left+xi*cell+(cell-4)/2}" y="{top+len(loads)*cell+20}" fill="#cbd5e1" text-anchor="middle" font-family="sans-serif">{speed:g}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#020617"/>
<text x="{width/2}" y="28" fill="#e2e8f0" text-anchor="middle" font-family="sans-serif" font-size="18">PMSM Operating Envelope</text><text x="{width/2}" y="48" fill="#94a3b8" text-anchor="middle" font-family="sans-serif">Green: within 5% speed error and safety limits</text>
{''.join(cells)}<text x="{left+len(speeds)*cell/2}" y="{top+len(loads)*cell+45}" fill="#94a3b8" text-anchor="middle" font-family="sans-serif">Target speed (RPM)</text><text x="20" y="{top+len(loads)*cell/2}" fill="#94a3b8" text-anchor="middle" font-family="sans-serif" transform="rotate(-90 20 {top+len(loads)*cell/2})">Load torque (N·m)</text></svg>'''


def simulate_pmsm(payload: Dict[str, Any], output_dir: Path, *, write_artifacts: bool = True) -> Dict[str, Any]:
    """Run a deterministic surface-PMSM dq model with cascaded PI control."""
    vdc = _positive(payload, "dc_bus_voltage_v", 48.0)
    target_rpm = _positive(payload, "rated_speed_rpm", 3000.0)
    loop_hz = _positive(payload, "control_loop_hz", 20_000.0)
    pole_pairs = int(_positive(payload, "pole_pairs", 4))
    rs = _positive(payload, "stator_resistance_ohm", 0.08)
    ld = _positive(payload, "ld_h", 0.0002)
    lq = _positive(payload, "lq_h", 0.0002)
    flux = _positive(payload, "flux_linkage_wb", 0.018)
    inertia = _positive(payload, "inertia_kg_m2", 0.0002)
    damping = max(0.0, float(payload.get("viscous_friction_nms", 0.00002)))
    load_torque = max(0.0, float(payload.get("load_torque_nm", 0.15)))
    duration = min(2.0, _positive(payload, "simulation_duration_s", 0.25))
    ambient = float(payload.get("ambient_temperature_c", 25.0))
    current_limit = _positive(payload, "current_limit_a", 15.0)

    dt = 1.0 / loop_hz
    steps = max(2, int(duration * loop_hz))
    voltage_limit = vdc / math.sqrt(3.0)
    torque_constant = 1.5 * pole_pairs * flux

    # Conservative, parameter-derived controller gains for a reproducible baseline.
    current_bandwidth = min(1000.0, loop_hz / 20.0) * 2.0 * math.pi
    kp_i, ki_i = ld * current_bandwidth, rs * current_bandwidth
    speed_bandwidth = 20.0 * 2.0 * math.pi
    kp_w = inertia * speed_bandwidth / torque_constant
    ki_w = kp_w * speed_bandwidth / 5.0

    id_a = iq_a = omega = 0.0
    int_d = int_q = int_speed = 0.0
    winding_temp = ambient
    thermal_r_c_per_w = 1.2
    thermal_tau_s = 30.0
    rows: List[Dict[str, float]] = []
    max_current = max_torque = max_temp = 0.0
    saturated_samples = 0

    for index in range(steps + 1):
        t = index * dt
        target_omega = target_rpm * 2.0 * math.pi / 60.0
        speed_error = target_omega - omega
        int_speed += speed_error * dt
        iq_ref = max(-current_limit, min(current_limit, kp_w * speed_error + ki_w * int_speed))
        id_ref = 0.0

        error_d, error_q = id_ref - id_a, iq_ref - iq_a
        int_d += error_d * dt
        int_q += error_q * dt
        omega_e = pole_pairs * omega
        vd = kp_i * error_d + ki_i * int_d - omega_e * lq * iq_a
        vq = kp_i * error_q + ki_i * int_q + omega_e * (ld * id_a + flux)
        magnitude = math.hypot(vd, vq)
        if magnitude > voltage_limit:
            scale = voltage_limit / magnitude
            vd, vq = vd * scale, vq * scale
            saturated_samples += 1

        did = (vd - rs * id_a + omega_e * lq * iq_a) / ld
        diq = (vq - rs * iq_a - omega_e * (ld * id_a + flux)) / lq
        id_a += did * dt
        iq_a += diq * dt
        torque = 1.5 * pole_pairs * (flux * iq_a + (ld - lq) * id_a * iq_a)
        omega += ((torque - load_torque - damping * omega) / inertia) * dt
        omega = max(0.0, omega)
        copper_loss = 1.5 * rs * (id_a * id_a + iq_a * iq_a)
        winding_temp += (((ambient + copper_loss * thermal_r_c_per_w) - winding_temp) / thermal_tau_s) * dt
        rpm = omega * 60.0 / (2.0 * math.pi)

        max_current = max(max_current, math.hypot(id_a, iq_a))
        max_torque = max(max_torque, abs(torque))
        max_temp = max(max_temp, winding_temp)
        if index % max(1, steps // 500) == 0 or index == steps:
            rows.append({"time_s": t, "speed_rpm": rpm, "id_a": id_a, "iq_a": iq_a,
                         "torque_nm": torque, "vd_v": vd, "vq_v": vq,
                         "winding_temperature_c": winding_temp})

    final_rpm = omega * 60.0 / (2.0 * math.pi)
    metrics = {
        "solver": "chiploop_pmsm_dq_equation_v1",
        "simulation_mode": "equation",
        "duration_s": duration,
        "time_step_s": dt,
        "simulated_steps": steps,
        "target_speed_rpm": target_rpm,
        "final_speed_rpm": final_rpm,
        "steady_state_speed_error_percent": abs(target_rpm - final_rpm) / target_rpm * 100.0,
        "maximum_current_a": max_current,
        "maximum_torque_nm": max_torque,
        "final_winding_temperature_c": winding_temp,
        "maximum_winding_temperature_c": max_temp,
        "voltage_saturation_percent": saturated_samples / (steps + 1) * 100.0,
        "limits": {"current_limit_a": current_limit, "dc_bus_voltage_v": vdc},
        "checks": {
            "finite_outputs": all(math.isfinite(v) for row in rows for v in row.values()),
            "current_within_limit": max_current <= current_limit * 1.05,
            "temperature_below_120c": max_temp < 120.0,
        },
    }

    if not write_artifacts:
        return {"metrics": metrics, "timeseries": rows, "files": {}}
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "equation_timeseries.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metrics_path = output_dir / "equation_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    speed_plot = output_dir / "speed_response.svg"
    speed_plot.write_text(_line_svg(rows, "time_s", "speed_rpm", "PMSM Speed Response", "Speed (RPM)"), encoding="utf-8")
    current_plot = output_dir / "current_response.svg"
    current_plot.write_text(_line_svg(rows, "time_s", "iq_a", "PMSM q-axis Current", "Iq (A)"), encoding="utf-8")
    return {"metrics": metrics, "timeseries": rows, "files": {"equation_metrics": str(metrics_path), "equation_timeseries": str(csv_path), "speed_response_plot": str(speed_plot), "current_response_plot": str(current_plot)}}


def run_operating_sweep(payload: Dict[str, Any], output_dir: Path) -> Dict[str, Any]:
    rated_speed = _positive(payload, "rated_speed_rpm", 3000.0)
    rated_load = max(0.01, float(payload.get("load_torque_nm", 0.15)))
    speeds = [round(rated_speed * factor, 6) for factor in (0.25, 0.5, 0.75, 1.0, 1.1)]
    loads = [round(rated_load * factor, 6) for factor in (0.0, 0.5, 1.0)]
    rows: List[Dict[str, Any]] = []
    for load in loads:
        for speed in speeds:
            case = {**payload, "rated_speed_rpm": speed, "load_torque_nm": load}
            metrics = simulate_pmsm(case, output_dir, write_artifacts=False)["metrics"]
            feasible = metrics["steady_state_speed_error_percent"] <= 5.0 and all(metrics["checks"].values())
            rows.append({"target_speed_rpm": speed, "load_torque_nm": load, "final_speed_rpm": metrics["final_speed_rpm"],
                         "speed_error_percent": metrics["steady_state_speed_error_percent"], "maximum_current_a": metrics["maximum_current_a"],
                         "voltage_saturation_percent": metrics["voltage_saturation_percent"], "feasible": feasible})
    csv_path = output_dir / "operating_sweep.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    json_path = output_dir / "operating_sweep.json"
    result = {"speed_points_rpm": speeds, "load_points_nm": loads, "cases": rows,
              "feasible_cases": sum(1 for row in rows if row["feasible"]), "total_cases": len(rows)}
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    plot_path = output_dir / "operating_envelope.svg"
    plot_path.write_text(_sweep_svg(rows, speeds, loads), encoding="utf-8")
    return {"result": result, "files": {"operating_sweep_csv": str(csv_path), "operating_sweep": str(json_path), "operating_envelope_plot": str(plot_path)}}
