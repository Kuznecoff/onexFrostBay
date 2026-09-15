"""Host machine telemetry (CPU temperature / load) for display alongside device data.

The Frostbay app primarily talks to a BLE cooling device, but it is often handy
to also see how hot the *host* machine's CPU is running while tuning that device.
This module reads that information with :mod:`psutil` and is fully defensive:
every reader returns ``None`` on any problem so the rest of the app keeps working
even if psutil is missing or the platform exposes no sensors (e.g. stock macOS).

Cross-platform notes:
  * Linux: real CPU temp via ``sensors_temperatures()`` (coretemp/k10temp/acpitz…).
  * Windows: usually available on modern laptops/motherboards.
  * macOS: :mod:`psutil` sensors are typically empty without root, so we fall back
    to CPU utilisation (clearly labelled as ``%``) rather than pretending it is a
    temperature.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Warm up cpu_percent() once at import: its first interval-based call returns 0.0.
try:  # pragma: no cover - import side effect
    import psutil

    if hasattr(psutil, "cpu_percent"):
        psutil.cpu_percent(interval=None)
except Exception:  # pragma: no cover - psutil optional
    psutil = None  # type: ignore


# Short cache so we don't hammer sensors on every render tick.
_cache: dict = {"value": None, "ts": 0.0}
_CACHE_TTL_SEC = 0.5


@dataclass
class HostStats:
    """Snapshot of host telemetry for the current moment."""

    cpu_temp_c: Optional[float] = None   # CPU temperature in °C when available
    cpu_load: Optional[float] = None     # 0-100 CPU utilisation (temp fallback)
    gpu_temp_c: Optional[float] = None   # GPU temperature in °C when available
    timestamp: float = 0.0               # unix seconds when this was read


# Labels / chip names that reliably indicate the main CPU thermal sensor.
# On AMD (incl. Strix Halo / Zen 5) the chip is ``k10temp`` and its entries are
# labelled ``Tctl`` / ``Tcase`` / ``Tsi`` -- the *chip* name, not the entry
# label, is what identifies it, so both are matched below.
_CPU_HINTS = (
    "cpu", "coretemp", "k10temp", "k10", "zenpower", "zen", "acpitz",
    "cpu_thermal", "cpu-thermal", "x86_pkg_temp", "soc_thermal", "strix",
)

# Labels / chip names that reliably indicate a GPU thermal sensor.
_GPU_HINTS = ("nvidia", "amdgpu", "dgpu", "i915", "intel_gpu", "gpu", "radeon", "nouveau")


def _classify(chip: str) -> str:
    """Classify an hwmon chip name as 'cpu', 'gpu' or '' (unknown)."""
    chip_l = (chip or "").lower()
    if any(h in chip_l for h in _GPU_HINTS):
        return "gpu"
    if any(h in chip_l for h in _CPU_HINTS) or chip_l.startswith(("cpu", "core", "k10", "zen")):
        return "cpu"
    return ""


def _read_sysfs_temps() -> tuple[Optional[float], Optional[float]]:
    """Fallback CPU/GPU temperature read straight from /sys hwmon nodes.

    psutil can return an empty sensor map on some distros/kernels even when
    ``/sys/class/hwmon`` exposes the sensors, which would otherwise hide the
    temperature behind a CPU-load percentage. Returns ``(cpu_c, gpu_c)``.
    """
    import glob
    import os

    cpu: Optional[float] = None
    gpu: Optional[float] = None
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(hwmon, "name")) as fh:
                kind = _classify(fh.read().strip())
        except OSError:
            continue
        if not kind:
            continue
        for tin in glob.glob(os.path.join(hwmon, "temp*_input")):
            try:
                with open(tin) as fh:
                    val = float(fh.read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if val <= 0:
                continue
            if kind == "gpu":
                if gpu is None or val > gpu:
                    gpu = val
            else:
                if cpu is None or val > cpu:
                    cpu = val
    return cpu, gpu


def _read_once() -> HostStats:
    temp: Optional[float] = None
    gpu_temp: Optional[float] = None
    load: Optional[float] = None

    if psutil is None:
        c, g = _read_sysfs_temps()
        return HostStats(cpu_temp_c=c, gpu_temp_c=g, timestamp=time.time())

    # Read temperature sensors. Tolerate platforms (e.g. stock macOS) that expose
    # no such API or sensor by falling through to CPU utilisation below, rather
    # than letting the AttributeError/ValueError abort the whole read.
    try:
        temps = psutil.sensors_temperatures() or {}
        for chip, entries in temps.items():
            kind = _classify(chip)
            for entry in entries:
                current = getattr(entry, "current", None)
                if not current:
                    continue
                label = (getattr(entry, "label", None) or "").lower()
                entry_gpu = kind == "gpu" or any(h in label for h in _GPU_HINTS)
                entry_cpu = kind == "cpu" or any(h in label for h in _CPU_HINTS)
                # GPU sensor takes precedence so we don't mistake it for the CPU.
                if entry_gpu:
                    if gpu_temp is None or float(current) > gpu_temp:
                        gpu_temp = float(current)
                    continue
                if entry_cpu:
                    if temp is None or float(current) > temp:
                        temp = float(current)
    except Exception:
        pass

    # Fallback: read hwmon directly from sysfs when psutil exposed nothing usable.
    if temp is None or gpu_temp is None:
        s_cpu, s_gpu = _read_sysfs_temps()
        if temp is None:
            temp = s_cpu
        if gpu_temp is None:
            gpu_temp = s_gpu

    # Fallback on platforms without a readable sensor (e.g. stock macOS):
    # surface CPU utilisation instead of nothing, clearly labelled as %.
    if temp is None and hasattr(psutil, "cpu_percent"):
        try:
            load = float(psutil.cpu_percent(interval=0.1))
        except Exception:
            load = None

    return HostStats(cpu_temp_c=temp, cpu_load=load, gpu_temp_c=gpu_temp, timestamp=time.time())


def get_host_stats() -> HostStats:
    """Return a cached :class:`HostStats` snapshot (refreshed at most ~2×/s)."""
    now = time.time()
    cached = _cache["value"]
    if cached is not None and (now - _cache["ts"]) < _CACHE_TTL_SEC:
        return cached
    value = _read_once()
    _cache["value"] = value
    _cache["ts"] = now
    return value


def format_cpu(stats: HostStats) -> str:
    """Short value string, e.g. ``47.3°C`` or ``23%`` (no leading label).

    Callers add their own context label (e.g. ``CPU 47.3°C``); keeping the value
    label-free avoids a redundant ``CPU CPU ...`` when several surfaces stack it
    next to a column header.
    """
    if stats.cpu_temp_c is not None:
        return f"{stats.cpu_temp_c:.1f}°C"
    if stats.cpu_load is not None:
        return f"{stats.cpu_load:.0f}%"
    return "n/a"


def format_gpu(stats: HostStats) -> str:
    """Short GPU temperature string, e.g. ``43.0°C`` (no leading label).

    Returns ``n/a`` when no GPU sensor is available on this platform.
    """
    if stats.gpu_temp_c is not None:
        return f"{stats.gpu_temp_c:.1f}°C"
    return "n/a"