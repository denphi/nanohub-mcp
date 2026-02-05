#!/usr/bin/env python
"""
Data Analysis MCP Server

Provides statistical analysis tools and sample datasets.

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
server = MCPServer("data-analysis", version="1.0.0")


# =============================================================================
# TOOLS
# =============================================================================

@server.tool()
def descriptive_stats(data):
    # type: (str) -> dict
    """
    Calculate descriptive statistics for a dataset.

    Args:
        data: Comma-separated list of numeric values (e.g., "1,2,3,4,5")

    Returns:
        Dictionary with mean, median, std, min, max, etc.
    """
    values = [float(x.strip()) for x in data.split(",")]
    n = len(values)
    if n == 0:
        return {"error": "Empty dataset"}

    sorted_data = sorted(values)
    mean = sum(values) / n

    # Median
    if n % 2 == 0:
        median = (sorted_data[n//2 - 1] + sorted_data[n//2]) / 2
    else:
        median = sorted_data[n//2]

    # Variance and standard deviation
    variance = sum((x - mean) ** 2 for x in values) / n
    std = math.sqrt(variance)

    return {
        "count": n,
        "mean": round(mean, 6),
        "median": round(median, 6),
        "min": min(values),
        "max": max(values),
        "range": max(values) - min(values),
        "std": round(std, 6),
        "sum": sum(values)
    }


@server.tool()
def correlation(x_data, y_data):
    # type: (str, str) -> dict
    """
    Calculate Pearson correlation coefficient between two datasets.

    Args:
        x_data: First comma-separated list of values
        y_data: Second comma-separated list of values

    Returns:
        Correlation coefficient and related statistics
    """
    x = [float(v.strip()) for v in x_data.split(",")]
    y = [float(v.strip()) for v in y_data.split(",")]

    n = len(x)
    if n != len(y):
        return {"error": "Datasets must have the same length"}
    if n < 2:
        return {"error": "Need at least 2 data points"}

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x) / n)
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / n)

    if std_x == 0 or std_y == 0:
        return {"error": "Standard deviation is zero"}

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
    """
    Perform simple linear regression (y = mx + b).

    Args:
        x_data: Independent variable values (comma-separated)
        y_data: Dependent variable values (comma-separated)

    Returns:
        Slope, intercept, and regression statistics
    """
    x = [float(v.strip()) for v in x_data.split(",")]
    y = [float(v.strip()) for v in y_data.split(",")]

    n = len(x)
    if n != len(y):
        return {"error": "Datasets must have the same length"}
    if n < 2:
        return {"error": "Need at least 2 data points"}

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    denominator = sum((xi - mean_x) ** 2 for xi in x)

    if denominator == 0:
        return {"error": "Cannot compute regression (x values are constant)"}

    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    # Calculate R-squared
    y_pred = [slope * xi + intercept for xi in x]
    ss_res = sum((y[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0

    return {
        "slope": round(slope, 6),
        "intercept": round(intercept, 6),
        "r_squared": round(r_squared, 6),
        "equation": "y = {}x + {}".format(round(slope, 4), round(intercept, 4)),
        "n": n
    }


@server.tool()
def normalize(data, method="minmax"):
    # type: (str, str) -> dict
    """
    Normalize a dataset.

    Args:
        data: Comma-separated list of values
        method: 'minmax' (0-1 range) or 'zscore' (mean=0, std=1)

    Returns:
        Normalized data and normalization parameters
    """
    values = [float(x.strip()) for x in data.split(",")]
    n = len(values)
    if n == 0:
        return {"error": "Empty dataset"}

    if method == "minmax":
        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val

        if range_val == 0:
            normalized = [0.5] * n
        else:
            normalized = [(x - min_val) / range_val for x in values]

        return {
            "normalized": [round(x, 6) for x in normalized],
            "method": "minmax",
            "min": min_val,
            "max": max_val
        }

    elif method == "zscore":
        mean = sum(values) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in values) / n)

        if std == 0:
            normalized = [0.0] * n
        else:
            normalized = [(x - mean) / std for x in values]

        return {
            "normalized": [round(x, 6) for x in normalized],
            "method": "zscore",
            "mean": round(mean, 6),
            "std": round(std, 6)
        }

    else:
        return {"error": "Unknown method. Use 'minmax' or 'zscore'"}


# =============================================================================
# RESOURCES
# =============================================================================

@server.resource("data://samples/temperatures", mime_type="application/json")
def temperature_data():
    """Monthly average temperatures (Celsius) for a year."""
    return {
        "description": "Monthly average temperatures",
        "unit": "Celsius",
        "data": [2.1, 3.5, 7.2, 12.1, 17.3, 21.5, 24.2, 23.8, 19.4, 13.2, 7.1, 3.2],
        "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    }


@server.resource("data://samples/scatter", mime_type="application/json")
def scatter_data():
    """Sample data for scatter plot / correlation analysis."""
    return {
        "description": "Study hours vs exam score",
        "x_label": "Study Hours",
        "y_label": "Exam Score",
        "x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "y": [52, 58, 65, 68, 72, 78, 82, 85, 90, 95]
    }


# =============================================================================
# PROMPTS
# =============================================================================

@server.prompt()
def analyze_data(data):
    # type: (str) -> list
    """Generate a prompt to analyze a dataset."""
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": "Please analyze this dataset and provide insights: {}".format(data)
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
