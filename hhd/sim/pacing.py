"""Echtzeitfaktor (Tempo) für Simulation und Wiedergabe, während eines Laufs änderbar (ab 0.15.0)."""
from __future__ import annotations

import time
from typing import Callable


class Paced:
    """Echtzeitfaktor ``speed``, der sich **während** eines Laufs ändern lässt (Tempo-Feld der App, ab 0.15.0).

    Fällig ist die Zeitscheibe bis ``t`` (Laufzeit, s) zur Wanduhrzeit ``_t_wall0 + t / speed``. Ändert sich ``speed``,
    wird der Anker so verschoben, dass die zuletzt erreichte Laufzeit ``_t_last`` jetzt fällig ist; ohne Verschiebung
    würde ein höheres Tempo die Wiedergabe springen lassen, ein niedrigeres sie anhalten.
    """
    _speed = 1.0
    _t_wall0: float | None = None
    _t_last = 0.0
    _pace_clock: Callable[[], float] = staticmethod(time.monotonic)

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, v: float) -> None:
        v = float(v)
        if not v > 0:
            raise ValueError("speed > 0")
        if self._t_wall0 is not None and v != self._speed:
            self._t_wall0 = self._pace_clock() - self._t_last / v
        self._speed = v

    def _pace_start(self) -> None:
        self._t_wall0, self._t_last = self._pace_clock(), 0.0

    def _pace_due(self, t: float) -> float:
        """Wanduhrzeit, zu der die Laufzeit ``t`` erreicht ist."""
        return (self._t_wall0 if self._t_wall0 is not None else self._pace_clock()) + t / self._speed
