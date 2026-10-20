"""Low-overhead Linux RSS diagnostics for live engine and dashboard.

This is *observability*, not a memory limit or an automatic stop condition.
A hard kill/engine shutdown would be dangerous while REAL positions are open.
"""
from __future__ import annotations

from pathlib import Path


def memory_usage_mib():
    """Return (current RSS MiB, peak RSS MiB), or (None, None) off Linux."""
    try:
        readings = {}
        with Path("/proc/self/status").open("r", encoding="ascii") as source:
            for line in source:
                if line.startswith("VmRSS:") or line.startswith("VmHWM:"):
                    key, text = line.split(":", 1)
                    readings[key] = int(text.split()[0]) / 1024.0
        return readings.get("VmRSS"), readings.get("VmHWM")
    except (OSError, ValueError, IndexError):
        return None, None
