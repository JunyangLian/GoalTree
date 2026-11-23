"""Injectable time source for simulated environments.

Default behavior is unchanged: a profile without a clock shows no simulated-time
line. Environments that model time provide a :class:`SimulatedClock` so text and
native prompts agree on the current instant instead of each hand-formatting the
scenario start time.
"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now(self) -> float:
        raise NotImplementedError

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class SystemClock(Clock):
    def now(self) -> float:
        return datetime.now(timezone.utc).timestamp()


class SimulatedClock(Clock):
    """A deterministic epoch the environment can advance between turns."""

    def __init__(self, epoch: float, step_seconds: float = 1.0):
        if type(epoch) not in {int, float}:
            raise ValueError("SimulatedClock epoch must be a number")
        if type(step_seconds) not in {int, float} or step_seconds <= 0:
            raise ValueError("SimulatedClock step_seconds must be positive")
        self._epoch = float(epoch)
        self._step = float(step_seconds)

    def now(self) -> float:
        return self._epoch

    def advance(self, seconds: float | None = None) -> float:
        delta = self._step if seconds is None else float(seconds)
        self._epoch += delta
        return self._epoch

    def tick(self) -> float:
        return self.advance()
