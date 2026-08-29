"""Ring-buffer history for live Frostbay telemetry.

Keeps the last N samples of the numeric parameters the device reports so the
dashboard can render rolling time-series: input/output water temperature, flow
rate, fan % and pump %. Thread-safe enough for single asyncio loop usage.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .protocol import FrostbayState


@dataclass
class Sample:
    t: float  # unix seconds
    temp_in_c: float
    temp_out_c: float
    flow_ml_min: float
    fan_percent: float
    pump_percent: float
    running: bool


class History:
    def __init__(self, maxlen: int = 120) -> None:
        self._lock = threading.Lock()
        self.temp_in: deque = deque(maxlen=maxlen)
        self.temp_out: deque = deque(maxlen=maxlen)
        self.flow: deque = deque(maxlen=maxlen)
        self.fan: deque = deque(maxlen=maxlen)
        self.pump: deque = deque(maxlen=maxlen)
        self.times: deque = deque(maxlen=maxlen)
        self.running: deque = deque(maxlen=maxlen)
        self._last: Sample | None = None

    def push(self, state: FrostbayState) -> Sample:
        now = time.time()
        s = Sample(
            t=now,
            temp_in_c=float(state.temp_in_c),
            temp_out_c=float(state.temp_out_c),
            flow_ml_min=float(state.flow_ml_min),
            fan_percent=float(state.fan_percent),
            pump_percent=float(state.pump_percent),
            running=state.is_running(),
        )
        with self._lock:
            self.times.append(now)
            self.temp_in.append(s.temp_in_c)
            self.temp_out.append(s.temp_out_c)
            self.flow.append(s.flow_ml_min)
            self.fan.append(s.fan_percent)
            self.pump.append(s.pump_percent)
            self.running.append(int(s.running))
            self._last = s
        return s

    def last(self) -> Sample | None:
        with self._lock:
            return self._last

    def series(self, name: str) -> list[float]:
        with self._lock:
            return list(getattr(self, name))

    def times_as_offsets(self) -> list[float]:
        with self._lock:
            if not self.times:
                return []
            last = self.times[-1]
            return [t - last for t in self.times]
