"""Kraft-Typen für die Physiksimulation.

Stellt verschiedene Kraftquellen bereit:
- Gravitation (nach unten gerichtet)
- Federkraft (Hookesches Gesetz)
- Luftwiderstand (Stokes-Reibung)
- Zentrierte Kraft (z. B. für Orbit-Simulation)
"""

from __future__ import annotations

from typing import Callable, List
from .body import Body


class Force:
    """Basis-Klasse für Kräfte.

    Eine Force ist ein Callable, das einen Kraft-Vektor (fx, fy)
    für einen gegebenen Körper berechnet.
    """

    def __call__(self, body: Body, dt: float) -> tuple:
        """Berechne (fx, fy) für den Körper. Überschreibe in Subklassen."""
        return (0.0, 0.0)


class Gravity(Force):
    """Konstante Gravitationskraft nach unten.

    F_g = m * g  (nach unten, also positive y-Richtung in Pygame-Koordinaten)
    """

    def __init__(self, g: float = 9.81 * 50):
        self.g = g

    def __call__(self, body: Body, dt: float) -> tuple:
        if body.static:
            return (0.0, 0.0)
        return (0.0, body.mass * self.g)


class SpringForce(Force):
    """Federkraft zwischen zwei Körpern (Hookesches Gesetz).

    F = -k * (distanz - ruhelänge)  in Richtung der Federachse.
    """

    def __init__(self, other: Body, rest_length: float, stiffness: float = 100.0):
        self.other = other
        self.rest_length = rest_length
        self.stiffness = stiffness

    def __call__(self, body: Body, dt: float) -> tuple:
        dx = self.other.x - body.x
        dy = self.other.y - body.y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance < 0.001:
            return (0.0, 0.0)
        displacement = distance - self.rest_length
        force_magnitude = -self.stiffness * displacement
        fx = force_magnitude * (dx / distance)
        fy = force_magnitude * (dy / distance)
        # Kraft auf beide Körper (entgegengesetzt) – wird extern gehandhabt
        return (fx, fy)


class PointGravity(Force):
    """Gravitation zu einem festen Punkt (z. B. für Orbit-Simulation).

    F = G * m1 * m2 / r²  in Richtung des Punktes.
    """

    def __init__(self, px: float, py: float, strength: float = 5000.0):
        self.px = px
        self.py = py
        self.strength = strength

    def __call__(self, body: Body, dt: float) -> tuple:
        dx = self.px - body.x
        dy = self.py - body.y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance < 1.0:
            return (0.0, 0.0)
        force = self.strength * body.mass / (distance * distance)
        fx = force * (dx / distance)
        fy = force * (dy / distance)
        return (fx, fy)


def drag_force(body: Body, dt: float, drag_coefficient: float = 0.01) -> tuple:
    """Luftwiderstand (geschwindigkeitsabhängige Reibung).

    F_drag = -c * v² * (normalisierte Richtung)
    """
    if body.static:
        return (0.0, 0.0)
    speed = (body.vx * body.vx + body.vy * body.vy) ** 0.5
    if speed < 0.001:
        return (0.0, 0.0)
    force = -drag_coefficient * speed * speed
    fx = force * (body.vx / speed)
    fy = force * (body.vy / speed)
    return (fx, fy)
