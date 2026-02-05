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

from nanohubmcp import MCPServer, Context

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

@server.tool()
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
        return {"error": "Invalid parameters"}

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


@server.tool()
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
        return {"error": "Provide exactly 3 of: pressure, volume, n_moles, temperature"}

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
        return {"error": "Velocity must be less than speed of light"}

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
