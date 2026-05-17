#!/usr/bin/env python
"""
Physics Simulator MCP Server

Provides tools for physics simulations and calculations.

Run with:
    start_mcp --app start_mcp.py
    start_mcp --app start_mcp.py --python-env AIIDA
"""

from __future__ import print_function

import math
import os
import sys

# Add package path for development
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nanohubmcp import MCPServer, Context, ToolResult

# Create server instance
server = MCPServer("physics-simulator", version="1.0.0")

# Physical constants
GRAVITY = 9.81  # m/s^2
SPEED_OF_LIGHT = 299792458  # m/s
PLANCK_CONSTANT = 6.62607015e-34  # J*s
BOLTZMANN_CONSTANT = 1.380649e-23  # J/K


# =============================================================================
# TOOLS
# =============================================================================

@server.tool(meta={"ui": {"resourceUri": "ui://physics-simulator/projectile",
                          "visibility": ["model", "app"]}})
def projectile_motion(v0, angle, h0=0):
    # type: (float, float, float) -> dict
    """
    Calculate projectile motion parameters.

    Args:
        v0: Initial velocity (m/s)
        angle: Launch angle (degrees)
        h0: Initial height (m), default 0

    Returns:
        Range, max height, time of flight, and trajectory points
    """
    v0 = float(v0)
    angle_rad = math.radians(float(angle))
    h0 = float(h0)

    vx = v0 * math.cos(angle_rad)
    vy = v0 * math.sin(angle_rad)

    # Time of flight (solving quadratic for when y = 0)
    # -0.5*g*t^2 + vy*t + h0 = 0
    discriminant = vy**2 + 2 * GRAVITY * h0
    if discriminant < 0:
        return ToolResult(content="Invalid parameters (negative discriminant)",
                          is_error=True)

    t_flight = (vy + math.sqrt(discriminant)) / GRAVITY

    # Range
    range_x = vx * t_flight

    # Maximum height
    t_max_height = vy / GRAVITY
    max_height = h0 + vy * t_max_height - 0.5 * GRAVITY * t_max_height**2

    # Generate trajectory points
    trajectory = []
    for i in range(21):
        t = t_flight * i / 20
        x = vx * t
        y = h0 + vy * t - 0.5 * GRAVITY * t**2
        trajectory.append({"t": round(t, 3), "x": round(x, 3), "y": round(y, 3)})

    return {
        "range": round(range_x, 3),
        "max_height": round(max_height, 3),
        "time_of_flight": round(t_flight, 3),
        "initial_velocity_x": round(vx, 3),
        "initial_velocity_y": round(vy, 3),
        "trajectory": trajectory
    }


@server.tool(meta={"ui": {"resourceUri": "ui://physics-simulator/oscillator",
                          "visibility": ["model", "app"]}})
def harmonic_oscillator(mass, spring_constant, amplitude, time):
    # type: (float, float, float, float) -> dict
    """
    Calculate simple harmonic motion parameters.

    Args:
        mass: Mass of oscillator (kg)
        spring_constant: Spring constant k (N/m)
        amplitude: Maximum displacement (m)
        time: Time point to evaluate (s)

    Returns:
        Position, velocity, acceleration, energy at given time
    """
    m = float(mass)
    k = float(spring_constant)
    A = float(amplitude)
    t = float(time)

    omega = math.sqrt(k / m)  # Angular frequency
    period = 2 * math.pi / omega
    frequency = 1 / period

    # Position, velocity, acceleration (assuming x = A*cos(omega*t))
    x = A * math.cos(omega * t)
    v = -A * omega * math.sin(omega * t)
    a = -A * omega**2 * math.cos(omega * t)

    # Energy
    kinetic = 0.5 * m * v**2
    potential = 0.5 * k * x**2
    total_energy = 0.5 * k * A**2

    return {
        "position": round(x, 6),
        "velocity": round(v, 6),
        "acceleration": round(a, 6),
        "angular_frequency": round(omega, 6),
        "period": round(period, 6),
        "frequency": round(frequency, 6),
        "kinetic_energy": round(kinetic, 6),
        "potential_energy": round(potential, 6),
        "total_energy": round(total_energy, 6)
    }


@server.tool()
def wave_properties(frequency, wavelength=None, medium_speed=None):
    # type: (float, float, float) -> dict
    """
    Calculate wave properties.

    Args:
        frequency: Wave frequency (Hz)
        wavelength: Wavelength (m), optional
        medium_speed: Speed in medium (m/s), optional (defaults to speed of light)

    Returns:
        Wave properties including period, speed, wavelength
    """
    f = float(frequency)

    if medium_speed is not None:
        v = float(medium_speed)
    elif wavelength is not None:
        v = f * float(wavelength)
    else:
        v = SPEED_OF_LIGHT

    if wavelength is not None:
        lam = float(wavelength)
    else:
        lam = v / f

    period = 1 / f
    wave_number = 2 * math.pi / lam
    angular_frequency = 2 * math.pi * f

    # Photon energy (if electromagnetic)
    photon_energy = PLANCK_CONSTANT * f

    return {
        "frequency": f,
        "wavelength": round(lam, 9),
        "speed": round(v, 3),
        "period": round(period, 9),
        "wave_number": round(wave_number, 6),
        "angular_frequency": round(angular_frequency, 6),
        "photon_energy_joules": photon_energy,
        "photon_energy_eV": round(photon_energy / 1.602176634e-19, 6)
    }


@server.tool()
def ideal_gas(pressure=None, volume=None, n_moles=None, temperature=None):
    # type: (float, float, float, float) -> dict
    """
    Ideal gas law calculator (PV = nRT).
    Provide 3 of the 4 variables to calculate the fourth.

    Args:
        pressure: Pressure (Pa)
        volume: Volume (m^3)
        n_moles: Amount of substance (mol)
        temperature: Temperature (K)

    Returns:
        All gas properties
    """
    R = 8.314462  # J/(mol*K)

    # Count provided values
    provided = sum(x is not None for x in [pressure, volume, n_moles, temperature])
    if provided != 3:
        return ToolResult(
            content="Provide exactly 3 of: pressure, volume, n_moles, temperature",
            is_error=True,
        )

    if pressure is None:
        P = float(n_moles) * R * float(temperature) / float(volume)
        V = float(volume)
        n = float(n_moles)
        T = float(temperature)
    elif volume is None:
        P = float(pressure)
        V = float(n_moles) * R * float(temperature) / P
        n = float(n_moles)
        T = float(temperature)
    elif n_moles is None:
        P = float(pressure)
        V = float(volume)
        T = float(temperature)
        n = P * V / (R * T)
    else:  # temperature is None
        P = float(pressure)
        V = float(volume)
        n = float(n_moles)
        T = P * V / (n * R)

    return {
        "pressure_Pa": round(P, 3),
        "pressure_atm": round(P / 101325, 6),
        "volume_m3": round(V, 9),
        "volume_L": round(V * 1000, 6),
        "n_moles": round(n, 6),
        "temperature_K": round(T, 3),
        "temperature_C": round(T - 273.15, 3)
    }


@server.tool(tags={"advanced"})
def relativistic_energy(ctx, rest_mass, velocity):
    # type: (Context, float, float) -> dict
    """
    Calculate relativistic energy and momentum.

    Args:
        rest_mass: Rest mass (kg)
        velocity: Velocity (m/s)

    Returns:
        Lorentz factor, relativistic mass, energy, momentum
    """
    m0 = float(rest_mass)
    v = float(velocity)

    if abs(v) >= SPEED_OF_LIGHT:
        return ToolResult(content="Velocity must be less than speed of light",
                          is_error=True)

    ctx.info("Calculating relativistic properties for v = {} m/s".format(v))

    beta = v / SPEED_OF_LIGHT
    gamma = 1 / math.sqrt(1 - beta**2)

    relativistic_mass = gamma * m0
    total_energy = gamma * m0 * SPEED_OF_LIGHT**2
    rest_energy = m0 * SPEED_OF_LIGHT**2
    kinetic_energy = total_energy - rest_energy
    momentum = gamma * m0 * v

    return {
        "lorentz_factor": round(gamma, 9),
        "beta": round(beta, 9),
        "relativistic_mass_kg": relativistic_mass,
        "rest_energy_J": rest_energy,
        "kinetic_energy_J": kinetic_energy,
        "total_energy_J": total_energy,
        "momentum_kg_m_s": momentum
    }


# =============================================================================
# MCP APPS (UI resources, per https://github.com/modelcontextprotocol/ext-apps)
# =============================================================================

_MCP_APP_MIME = "text/html;profile=mcp-app"

# Shared JS bridge: every app talks to the host via JSON-RPC over postMessage.
# `tool-result` notifications carry structuredContent (preferred) and a
# `content` array as a text fallback when structured data is absent.
#
# The HTML templates embed this bridge by interpolating the literal token
# `__BRIDGE__` (see `.replace("__BRIDGE__", _MCP_BRIDGE_JS)` below). Don't put
# that string anywhere else in the HTML or it will be substituted too.
#
# NOTE on `postMessage` origins: this example uses `'*'` for simplicity. In
# production, scope the target origin to the host-allocated sandbox domain
# advertised via `_meta.ui.domain` (see the mcp-apps spec). `'*'` is fine for
# local dev but lets any embedding frame receive messages this view sends.
_MCP_BRIDGE_JS = r"""
<script>
  let _nextId = 1;
  const _pending = new Map();
  // Example only — see the Python comment above about scoping this origin.
  const _hostOrigin = "*";
  function rpc(method, params) {
    const id = _nextId++;
    return new Promise((resolve, reject) => {
      _pending.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params: params || {} }, _hostOrigin);
    });
  }
  function notify(method, params) {
    window.parent.postMessage({ jsonrpc: "2.0", method, params: params || {} }, _hostOrigin);
  }
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.id !== undefined && _pending.has(msg.id)) {
      const { resolve, reject } = _pending.get(msg.id);
      _pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message || "RPC error"));
      else resolve(msg.result);
      return;
    }
    if (msg.method === "ui/notifications/tool-result" && typeof window.onToolResult === "function") {
      window.onToolResult(msg.params || {});
    }
    if (msg.method === "ui/notifications/tool-input" && typeof window.onToolInput === "function") {
      window.onToolInput(msg.params || {});
    }
  });
  function extractResult(params) {
    if (params && params.structuredContent) return params.structuredContent;
    const content = (params && params.content) || [];
    for (const item of content) {
      if (item.type === "text") {
        try { return JSON.parse(item.text); } catch (_) { return { text: item.text }; }
      }
    }
    return {};
  }
  async function run(tool, args) {
    const res = await rpc("tools/call", { name: tool, arguments: args });
    return extractResult(res);
  }
  // True when this view is embedded by an mcp-apps host. Apps should wait for
  // `ui/notifications/tool-result` instead of eagerly calling the tool, so they
  // don't race the host's first invocation.
  const isEmbedded = window.parent !== window;
  function reportSize() {
    notify("ui/notifications/size-changed", {
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
    });
  }
  rpc("ui/initialize", {
    capabilities: {},
    clientInfo: { name: "physics-simulator-app", version: "1.0.0" },
    protocolVersion: "2026-01-26",
  }).then(() => notify("ui/notifications/initialized", {}))
    .catch(() => {});
  new ResizeObserver(reportSize).observe(document.documentElement);
</script>
""".strip()

_PROJECTILE_APP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Projectile Motion</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0b1020;color:#e6ecff}
 h2{margin:0 0 12px 0;font-size:18px}
 form{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;align-items:center;margin-bottom:12px}
 input{background:#1a2244;color:#e6ecff;border:1px solid #2a3470;border-radius:4px;padding:6px}
 button{background:#4f7cff;color:#fff;border:0;border-radius:4px;padding:8px 14px;cursor:pointer;grid-column:1/-1}
 button:hover{background:#3d68e8}
 canvas{background:#070b1a;border:1px solid #2a3470;border-radius:4px;display:block;width:100%;height:260px}
 .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px;font-size:13px}
 .stat{background:#141a35;padding:8px;border-radius:4px}
 .stat b{display:block;color:#8aa0ff;font-weight:500;font-size:11px;text-transform:uppercase}
</style></head>
<body>
  <h2>Projectile Motion</h2>
  <form id="f">
    <label for="v0">v<sub>0</sub> (m/s)</label><input id="v0" type="number" value="30" step="1">
    <label for="angle">angle (deg)</label><input id="angle" type="number" value="45" step="1">
    <label for="h0">h<sub>0</sub> (m)</label><input id="h0" type="number" value="0" step="0.5">
    <button type="submit">Simulate</button>
  </form>
  <canvas id="plot" width="600" height="260"></canvas>
  <div class="stats">
    <div class="stat"><b>Range</b><span id="r">—</span> m</div>
    <div class="stat"><b>Max height</b><span id="h">—</span> m</div>
    <div class="stat"><b>Flight time</b><span id="t">—</span> s</div>
  </div>
__BRIDGE__
<script>
  const $ = (id) => document.getElementById(id);
  function draw(traj){
    const c = $("plot"), ctx = c.getContext("2d");
    ctx.clearRect(0,0,c.width,c.height);
    if (!traj || !traj.length) return;
    const xs = traj.map(p=>p.x), ys = traj.map(p=>p.y);
    const xmax = Math.max(...xs, 1), ymax = Math.max(...ys, 1);
    const pad = 24;
    const sx = (c.width - 2*pad) / xmax;
    const sy = (c.height - 2*pad) / ymax;
    ctx.strokeStyle = "#2a3470"; ctx.beginPath();
    ctx.moveTo(pad, c.height-pad); ctx.lineTo(c.width-pad, c.height-pad);
    ctx.moveTo(pad, c.height-pad); ctx.lineTo(pad, pad); ctx.stroke();
    ctx.strokeStyle = "#4f7cff"; ctx.lineWidth = 2; ctx.beginPath();
    traj.forEach((p,i) => {
      const x = pad + p.x*sx, y = c.height - pad - p.y*sy;
      if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  function render(r){
    if (!r || !r.trajectory) return;
    $("r").textContent = r.range;
    $("h").textContent = r.max_height;
    $("t").textContent = r.time_of_flight;
    draw(r.trajectory);
  }
  async function simulate(){
    const args = { v0: +$("v0").value, angle: +$("angle").value, h0: +$("h0").value };
    render(await run("projectile_motion", args));
  }
  window.onToolResult = (params) => render(extractResult(params));
  $("f").addEventListener("submit", (e) => { e.preventDefault(); simulate(); });
  // Standalone preview: kick off a default run. Embedded under an mcp-apps
  // host: wait for the host to deliver the tool result via tool-result.
  if (!isEmbedded) simulate();
</script>
</body></html>
""".replace("__BRIDGE__", _MCP_BRIDGE_JS)


_OSCILLATOR_APP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Harmonic Oscillator</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0b1020;color:#e6ecff}
 h2{margin:0 0 12px 0;font-size:18px}
 form{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;align-items:center;margin-bottom:12px}
 input{background:#1a2244;color:#e6ecff;border:1px solid #2a3470;border-radius:4px;padding:6px}
 button{background:#22c55e;color:#03110a;font-weight:600;border:0;border-radius:4px;padding:8px 14px;cursor:pointer;grid-column:1/-1}
 canvas{background:#070b1a;border:1px solid #2a3470;border-radius:4px;display:block;width:100%;height:240px}
 .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px;font-size:13px}
 .stat{background:#141a35;padding:8px;border-radius:4px}
 .stat b{display:block;color:#86efac;font-weight:500;font-size:11px;text-transform:uppercase}
</style></head>
<body>
  <h2>Simple Harmonic Oscillator</h2>
  <form id="f">
    <label for="m">mass (kg)</label><input id="m" type="number" value="1" step="0.1" min="0.01">
    <label for="k">k (N/m)</label><input id="k" type="number" value="10" step="1" min="0.01">
    <label for="A">amplitude (m)</label><input id="A" type="number" value="0.2" step="0.05">
    <button type="submit">Simulate one period</button>
  </form>
  <canvas id="plot" width="600" height="240"></canvas>
  <div class="stats">
    <div class="stat"><b>Period T</b><span id="T">—</span> s</div>
    <div class="stat"><b>ω</b><span id="w">—</span> rad/s</div>
    <div class="stat"><b>Total E</b><span id="E">—</span> J</div>
  </div>
__BRIDGE__
<script>
  const $ = (id) => document.getElementById(id);
  function plotSeries(series){
    const c = $("plot"), ctx = c.getContext("2d");
    ctx.clearRect(0,0,c.width,c.height);
    const pad = 24;
    const ts = series.map(p=>p.t), xs = series.map(p=>p.x);
    const tmax = Math.max(...ts, 0.001);
    const amax = Math.max(...xs.map(Math.abs), 0.001);
    const sx = (c.width - 2*pad)/tmax;
    const sy = (c.height/2 - pad)/amax;
    const midY = c.height/2;
    ctx.strokeStyle = "#2a3470"; ctx.beginPath();
    ctx.moveTo(pad, midY); ctx.lineTo(c.width-pad, midY); ctx.stroke();
    ctx.strokeStyle = "#22c55e"; ctx.lineWidth = 2; ctx.beginPath();
    series.forEach((p,i) => {
      const x = pad + p.t*sx;
      const y = midY - p.x*sy;
      if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  function render(r){
    if (!r || !r.period) return;
    $("T").textContent = r.period.toFixed(4);
    $("w").textContent = r.angular_frequency.toFixed(4);
    $("E").textContent = r.total_energy.toFixed(6);
    // Sample one period analytically using the same formula as the server tool
    const w = r.angular_frequency, T = r.period, A = +$("A").value;
    const steps = 60, series = [];
    for (let i=0; i<=steps; i++){
      const t = T * i / steps;
      series.push({ t, x: A*Math.cos(w*t) });
    }
    plotSeries(series);
  }
  async function simulate(){
    const m = +$("m").value, k = +$("k").value, A = +$("A").value;
    render(await run("harmonic_oscillator", { mass:m, spring_constant:k, amplitude:A, time:0 }));
  }
  window.onToolResult = (params) => render(extractResult(params));
  $("f").addEventListener("submit", (e) => { e.preventDefault(); simulate(); });
  // Standalone preview: kick off a default run. Embedded under an mcp-apps
  // host: wait for the host to deliver the tool result via tool-result.
  if (!isEmbedded) simulate();
</script>
</body></html>
""".replace("__BRIDGE__", _MCP_BRIDGE_JS)


def _default_app_meta():
    # Build a fresh meta dict per call so each registered resource owns its
    # nested csp/permissions objects (no shared mutable state across apps).
    return {
        "ui": {
            "csp": {
                "connectDomains": [],
                "resourceDomains": [],
                "frameDomains": [],
                "baseUriDomains": [],
            },
            "permissions": {},
            "prefersBorder": True,
        }
    }


@server.resource(
    "ui://physics-simulator/projectile",
    mime_type=_MCP_APP_MIME,
    description="Interactive projectile-motion simulator UI",
    meta=_default_app_meta(),
)
def projectile_app():
    """MCP App: projectile motion simulator UI."""
    return _PROJECTILE_APP_HTML


@server.resource(
    "ui://physics-simulator/oscillator",
    mime_type=_MCP_APP_MIME,
    description="Interactive harmonic-oscillator simulator UI",
    meta=_default_app_meta(),
)
def oscillator_app():
    """MCP App: simple-harmonic-oscillator simulator UI."""
    return _OSCILLATOR_APP_HTML


# =============================================================================
# RESOURCES
# =============================================================================

@server.resource("constants://physics", mime_type="application/json")
def physical_constants():
    """Fundamental physical constants."""
    return {
        "speed_of_light": {"value": SPEED_OF_LIGHT, "unit": "m/s", "symbol": "c"},
        "gravitational_acceleration": {"value": GRAVITY, "unit": "m/s^2", "symbol": "g"},
        "planck_constant": {"value": PLANCK_CONSTANT, "unit": "J*s", "symbol": "h"},
        "boltzmann_constant": {"value": BOLTZMANN_CONSTANT, "unit": "J/K", "symbol": "k_B"},
        "gas_constant": {"value": 8.314462, "unit": "J/(mol*K)", "symbol": "R"},
        "avogadro_number": {"value": 6.02214076e23, "unit": "1/mol", "symbol": "N_A"},
        "electron_mass": {"value": 9.1093837015e-31, "unit": "kg", "symbol": "m_e"},
        "proton_mass": {"value": 1.67262192369e-27, "unit": "kg", "symbol": "m_p"},
        "elementary_charge": {"value": 1.602176634e-19, "unit": "C", "symbol": "e"}
    }


@server.resource("config://simulator/settings", mime_type="application/json")
def simulator_settings():
    """Simulator configuration settings."""
    return {
        "precision": 6,
        "default_gravity": GRAVITY,
        "supported_simulations": [
            "projectile_motion",
            "harmonic_oscillator",
            "wave_properties",
            "ideal_gas",
            "relativistic_energy"
        ]
    }


# =============================================================================
# PROMPTS
# =============================================================================

@server.prompt()
def physics_problem(problem_description):
    # type: (str) -> list
    """Generate a prompt to solve a physics problem."""
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": "Please solve this physics problem step by step: {}".format(problem_description)
            }
        }
    ]


def main():
    port = int(os.environ.get("MCP_PORT", 8000))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    server.run(port=port)


if __name__ == "__main__":
    main()
