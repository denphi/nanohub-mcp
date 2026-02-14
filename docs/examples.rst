Examples
========

Simple Calculator
-----------------

A basic calculator with arithmetic operations, a settings resource, and a calculation prompt.

.. code-block:: python

   from nanohubmcp import MCPServer, Context

   server = MCPServer("simple-calculator", version="1.0.0")

   @server.tool()
   def add(a, b):
       # type: (float, float) -> float
       """Add two numbers together."""
       return float(a) + float(b)

   @server.tool(tags={"math", "advanced"})
   def power(ctx, base, exponent):
       # type: (Context, float, float) -> float
       """Raise base to the power of exponent. Demonstrates Context usage."""
       ctx.info("Computing {}^{}".format(base, exponent))
       return float(base) ** float(exponent)

   @server.tool()
   def subtract(a, b):
       # type: (float, float) -> float
       """Subtract b from a."""
       return float(a) - float(b)

   @server.tool()
   def multiply(a, b):
       # type: (float, float) -> float
       """Multiply two numbers."""
       return float(a) * float(b)

   @server.tool()
   def divide(a, b):
       # type: (float, float) -> float
       """Divide a by b."""
       if float(b) == 0:
           raise ValueError("Cannot divide by zero")
       return float(a) / float(b)

   @server.resource("config://calculator/settings")
   def get_settings():
       """Get calculator settings."""
       return {
           "precision": 10,
           "max_value": 1e308,
           "supported_operations": ["add", "subtract", "multiply", "divide", "power"]
       }

   @server.prompt()
   def calculate(expression):
       # type: (str) -> list
       """Generate a calculation prompt."""
       return [
           {
               "role": "user",
               "content": {"type": "text", "text": "Please calculate: {}".format(expression)}
           }
       ]

   if __name__ == "__main__":
       server.run(port=8000)

Test it:

.. code-block:: bash

   # Call add
   curl -X POST http://localhost:8000/tools/add \
     -H "Content-Type: application/json" -d '{"a": 2, "b": 3}'

   # Call power (uses Context for logging)
   curl -X POST http://localhost:8000/tools/power \
     -H "Content-Type: application/json" -d '{"base": 2, "exponent": 10}'

   # Call divide (error case)
   curl -X POST http://localhost:8000/tools/divide \
     -H "Content-Type: application/json" -d '{"a": 1, "b": 0}'

Full source: ``examples/simple/start_mcp.py``

**Sample prompts to try with an AI client:**

   *"What is 1234 raised to the power of 5?"*

   *"Divide 100 by 7 and then multiply the result by 3."*

   *"What happens when I try to divide 42 by zero?"*


Data Analysis
-------------

Statistical analysis tools with sample datasets for data exploration.

.. code-block:: python

   import math
   from nanohubmcp import MCPServer

   server = MCPServer("data-analysis", version="1.0.0")

   @server.tool()
   def descriptive_stats(data):
       # type: (str) -> dict
       """
       Calculate descriptive statistics for a dataset.

       Args:
           data: Comma-separated list of numeric values (e.g., "1,2,3,4,5")
       """
       values = [float(x.strip()) for x in data.split(",")]
       n = len(values)
       sorted_data = sorted(values)
       mean = sum(values) / n

       if n % 2 == 0:
           median = (sorted_data[n//2 - 1] + sorted_data[n//2]) / 2
       else:
           median = sorted_data[n//2]

       variance = sum((x - mean) ** 2 for x in values) / n
       std = math.sqrt(variance)

       return {
           "count": n, "mean": round(mean, 6), "median": round(median, 6),
           "min": min(values), "max": max(values), "std": round(std, 6)
       }

   @server.tool()
   def correlation(x_data, y_data):
       # type: (str, str) -> dict
       """
       Calculate Pearson correlation coefficient between two datasets.

       Args:
           x_data: First comma-separated list of values
           y_data: Second comma-separated list of values
       """
       x = [float(v.strip()) for v in x_data.split(",")]
       y = [float(v.strip()) for v in y_data.split(",")]
       n = len(x)
       mean_x = sum(x) / n
       mean_y = sum(y) / n
       cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
       std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x) / n)
       std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / n)
       r = cov / (std_x * std_y)
       return {
           "correlation_coefficient": round(r, 6),
           "r_squared": round(r ** 2, 6),
           "covariance": round(cov, 6),
           "n": n
       }

   @server.tool()
   def linear_regression(x_data, y_data):
       # type: (str, str) -> dict
       """Perform simple linear regression (y = mx + b)."""
       x = [float(v.strip()) for v in x_data.split(",")]
       y = [float(v.strip()) for v in y_data.split(",")]
       n = len(x)
       mean_x = sum(x) / n
       mean_y = sum(y) / n
       numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
       denominator = sum((xi - mean_x) ** 2 for xi in x)
       slope = numerator / denominator
       intercept = mean_y - slope * mean_x
       return {
           "slope": round(slope, 6),
           "intercept": round(intercept, 6),
           "equation": "y = {}x + {}".format(round(slope, 4), round(intercept, 4))
       }

   @server.tool()
   def normalize(data, method="minmax"):
       # type: (str, str) -> dict
       """Normalize a dataset using 'minmax' or 'zscore' method."""
       values = [float(x.strip()) for x in data.split(",")]
       if method == "minmax":
           min_val, max_val = min(values), max(values)
           normalized = [(x - min_val) / (max_val - min_val) for x in values]
           return {"normalized": [round(x, 6) for x in normalized], "method": "minmax"}
       elif method == "zscore":
           mean = sum(values) / len(values)
           std = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))
           normalized = [(x - mean) / std for x in values]
           return {"normalized": [round(x, 6) for x in normalized], "method": "zscore"}

   @server.resource("data://samples/temperatures", mime_type="application/json")
   def temperature_data():
       """Monthly average temperatures (Celsius) for a year."""
       return {
           "data": [2.1, 3.5, 7.2, 12.1, 17.3, 21.5, 24.2, 23.8, 19.4, 13.2, 7.1, 3.2],
           "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
       }

   @server.resource("data://samples/scatter", mime_type="application/json")
   def scatter_data():
       """Sample data for scatter plot / correlation analysis."""
       return {
           "x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
           "y": [52, 58, 65, 68, 72, 78, 82, 85, 90, 95]
       }

   @server.prompt()
   def analyze_data(data):
       # type: (str) -> list
       """Generate a prompt to analyze a dataset."""
       return [{"role": "user", "content": {"type": "text", "text": "Please analyze: {}".format(data)}}]

   if __name__ == "__main__":
       server.run(port=8000)

Test it:

.. code-block:: bash

   # Descriptive statistics
   curl -X POST http://localhost:8000/tools/descriptive_stats \
     -H "Content-Type: application/json" \
     -d '{"data": "10,20,30,40,50"}'

   # Correlation
   curl -X POST http://localhost:8000/tools/correlation \
     -H "Content-Type: application/json" \
     -d '{"x_data": "1,2,3,4,5", "y_data": "2,4,6,8,10"}'

   # Linear regression
   curl -X POST http://localhost:8000/tools/linear_regression \
     -H "Content-Type: application/json" \
     -d '{"x_data": "1,2,3,4,5", "y_data": "2.1,3.9,6.2,7.8,10.1"}'

   # Normalize with z-score
   curl -X POST http://localhost:8000/tools/normalize \
     -H "Content-Type: application/json" \
     -d '{"data": "10,20,30,40,50", "method": "zscore"}'

   # Read temperature dataset
   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"data://samples/temperatures"}}'

Full source: ``examples/data_analysis/start_mcp.py``

**Sample prompts to try with an AI client:**

   *"Calculate the mean, median and standard deviation for: 4, 8, 15, 16, 23, 42"*

   *"What is the Pearson correlation between these two datasets: x = 1,2,3,4,5 and y = 2.1, 3.9, 6.2, 7.8, 10.1? Is there a strong relationship?"*

   *"Normalize the dataset 10, 50, 200, 500, 1000 using z-score and explain what the values mean."*

   *"Read the temperature dataset and tell me which month is the hottest and coldest, and fit a linear regression to the data."*


Physics Simulator
-----------------

Physics simulation tools with physical constants as resources.

.. code-block:: python

   import math
   from nanohubmcp import MCPServer, Context

   server = MCPServer("physics-simulator", version="1.0.0")

   GRAVITY = 9.81
   SPEED_OF_LIGHT = 299792458
   PLANCK_CONSTANT = 6.62607015e-34
   BOLTZMANN_CONSTANT = 1.380649e-23

   @server.tool()
   def projectile_motion(v0, angle, h0=0):
       # type: (float, float, float) -> dict
       """
       Calculate projectile motion parameters.

       Args:
           v0: Initial velocity (m/s)
           angle: Launch angle (degrees)
           h0: Initial height (m), default 0
       """
       v0 = float(v0)
       angle_rad = math.radians(float(angle))
       h0 = float(h0)
       vx = v0 * math.cos(angle_rad)
       vy = v0 * math.sin(angle_rad)
       discriminant = vy**2 + 2 * GRAVITY * h0
       t_flight = (vy + math.sqrt(discriminant)) / GRAVITY
       range_x = vx * t_flight
       t_max_height = vy / GRAVITY
       max_height = h0 + vy * t_max_height - 0.5 * GRAVITY * t_max_height**2
       return {
           "range": round(range_x, 3),
           "max_height": round(max_height, 3),
           "time_of_flight": round(t_flight, 3)
       }

   @server.tool()
   def harmonic_oscillator(mass, spring_constant, amplitude, time):
       # type: (float, float, float, float) -> dict
       """Calculate simple harmonic motion parameters."""
       m, k, A, t = float(mass), float(spring_constant), float(amplitude), float(time)
       omega = math.sqrt(k / m)
       x = A * math.cos(omega * t)
       v = -A * omega * math.sin(omega * t)
       return {
           "position": round(x, 6),
           "velocity": round(v, 6),
           "angular_frequency": round(omega, 6),
           "period": round(2 * math.pi / omega, 6),
           "total_energy": round(0.5 * k * A**2, 6)
       }

   @server.tool()
   def wave_properties(frequency, wavelength=None, medium_speed=None):
       # type: (float, float, float) -> dict
       """Calculate wave properties (period, speed, wavelength, photon energy)."""
       f = float(frequency)
       if medium_speed is not None:
           v = float(medium_speed)
       elif wavelength is not None:
           v = f * float(wavelength)
       else:
           v = SPEED_OF_LIGHT
       lam = float(wavelength) if wavelength is not None else v / f
       return {
           "frequency": f,
           "wavelength": round(lam, 9),
           "speed": round(v, 3),
           "period": round(1 / f, 9),
           "photon_energy_eV": round(PLANCK_CONSTANT * f / 1.602176634e-19, 6)
       }

   @server.tool()
   def ideal_gas(pressure=None, volume=None, n_moles=None, temperature=None):
       # type: (float, float, float, float) -> dict
       """Ideal gas law calculator (PV = nRT). Provide 3 of 4 variables."""
       R = 8.314462
       if pressure is None:
           P = float(n_moles) * R * float(temperature) / float(volume)
       elif volume is None:
           P = float(pressure)
           P, V = P, float(n_moles) * R * float(temperature) / P
       # ... (see full source for all cases)
       return {"pressure_Pa": round(P, 3), "volume_m3": round(V, 9)}

   @server.tool(tags={"advanced"})
   def relativistic_energy(ctx, rest_mass, velocity):
       # type: (Context, float, float) -> dict
       """Calculate relativistic energy and momentum."""
       ctx.info("Calculating relativistic properties for v = {} m/s".format(velocity))
       m0, v = float(rest_mass), float(velocity)
       beta = v / SPEED_OF_LIGHT
       gamma = 1 / math.sqrt(1 - beta**2)
       return {
           "lorentz_factor": round(gamma, 9),
           "total_energy_J": gamma * m0 * SPEED_OF_LIGHT**2,
           "kinetic_energy_J": (gamma - 1) * m0 * SPEED_OF_LIGHT**2
       }

   @server.resource("constants://physics", mime_type="application/json")
   def physical_constants():
       """Fundamental physical constants."""
       return {
           "speed_of_light": {"value": SPEED_OF_LIGHT, "unit": "m/s"},
           "gravitational_acceleration": {"value": GRAVITY, "unit": "m/s^2"},
           "planck_constant": {"value": PLANCK_CONSTANT, "unit": "J*s"},
           "boltzmann_constant": {"value": BOLTZMANN_CONSTANT, "unit": "J/K"}
       }

   @server.prompt()
   def physics_problem(problem_description):
       # type: (str) -> list
       """Generate a prompt to solve a physics problem."""
       return [{"role": "user", "content": {"type": "text", "text": "Solve: {}".format(problem_description)}}]

   if __name__ == "__main__":
       server.run(port=8000)

Test it:

.. code-block:: bash

   # Projectile motion (50 m/s at 45 degrees)
   curl -X POST http://localhost:8000/tools/projectile_motion \
     -H "Content-Type: application/json" \
     -d '{"v0": 50, "angle": 45}'

   # Harmonic oscillator
   curl -X POST http://localhost:8000/tools/harmonic_oscillator \
     -H "Content-Type: application/json" \
     -d '{"mass": 0.5, "spring_constant": 20, "amplitude": 0.1, "time": 1.0}'

   # Ideal gas law (find pressure given V, n, T)
   curl -X POST http://localhost:8000/tools/ideal_gas \
     -H "Content-Type: application/json" \
     -d '{"volume": 0.0224, "n_moles": 1, "temperature": 273.15}'

   # Wave properties (visible light at 500 THz)
   curl -X POST http://localhost:8000/tools/wave_properties \
     -H "Content-Type: application/json" \
     -d '{"frequency": 5e14}'

   # Read physical constants
   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"constants://physics"}}'

Full source: ``examples/simulator/start_mcp.py``

**Sample prompts to try with an AI client:**

   *"A soccer player kicks a ball with an initial velocity of 25 m/s at an angle of 35 degrees.
   Calculate how far away the ball will land (range) and the maximum height it reaches."*

   *"Calculate the relativistic energy and momentum for a proton traveling at 90% the speed of
   light (velocity = 269,813,212 m/s). What is its Lorentz factor?"*

   *"I have a balloon with 2 moles of Helium gas. The pressure inside is 101325 Pa (1 atm) and
   the temperature is 25 degrees Celsius (298.15 K). What is the volume of the balloon in liters?"*

   *"A sound wave in air has a frequency of 440 Hz (Note A4). Assuming the speed of sound is
   343 m/s, what is the wavelength and period of this wave?"*

   *"Analyze a simple harmonic oscillator with a 2 kg mass attached to a spring with a constant
   of 50 N/m. The maximum displacement (amplitude) is 0.5 meters. What is the position, velocity,
   and total energy of the system at time t = 1.5 seconds?"*

   *"Read the* ``physical_constants`` *resource to tell me the exact value of the Boltzmann
   constant and the Planck constant used by the simulator."*

   *"First, calculate the time of flight for a projectile launched at 30 m/s at 45 degrees. Then,
   use that total flight time as the time input for a harmonic oscillator with mass=1 kg,
   spring_constant=10, amplitude=1. What is the position of the oscillator at the exact moment
   the projectile hits the ground?"*

   *"Calculate the volume of 1 mole of an ideal gas at 300 K and 100,000 Pa. Then, if we treat a
   single gas particle as having a rest mass of 6.64e-27 kg (Helium-4) moving at 1000 m/s, what
   is its relativistic kinetic energy?"*
