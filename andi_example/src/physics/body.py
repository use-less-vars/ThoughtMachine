"""Physikalische Körper für die 2D-Simulation.

Definiert Basis-Körper (rund, eckig) mit:
- Position, Geschwindigkeit, Beschleunigung
- Masse, Dichte, Elastizität
- Kollisions-Eigenschaften (Radius, AABB)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple, Optional
import math


@dataclass
class Body:
    """Basis-Klasse für alle physikalischen Körper."""

    # Position & Bewegung
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    ax: float = 0.0
    ay: float = 0.0

    # Physikalische Eigenschaften
    mass: float = 1.0
    elasticity: float = 0.7  # Restitutionskoeffizient (0–1)
    friction: float = 0.3    # Reibungskoeffizient (0–1)
    static: bool = False     # True = unbeweglich (z. B. Boden, Wände)

    # Visuelles
    color: Tuple[int, int, int] = (100, 149, 237)
    label: str = ""

    # Tracking
    active: bool = True

    @property
    def inv_mass(self) -> float:
        """Inverse Masse (0 für statische Körper)."""
        return 0.0 if self.static else (1.0 / self.mass)

    def apply_force(self, fx: float, fy: float) -> None:
        """Kraft auf den Körper anwenden (F = m * a → a += F/m)."""
        if self.static:
            return
        self.ax += fx * self.inv_mass
        self.ay += fy * self.inv_mass

    def apply_impulse(self, jx: float, jy: float) -> None:
        """Impuls sofort anwenden (Geschwindigkeitsänderung)."""
        if self.static:
            return
        self.vx += jx * self.inv_mass
        self.vy += jy * self.inv_mass

    def update(self, dt: float) -> None:
        """Integration: Euler-Vorwärts für einen Zeitschritt."""
        if self.static or not self.active:
            return
        self.vx += self.ax * dt
        self.vy += self.ay * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        # Beschleunigung zurücksetzen (Kräfte werden jeden Frame neu angewandt)
        self.ax = 0.0
        self.ay = 0.0

    def kinetic_energy(self) -> float:
        """Kinetische Energie: E_kin = 0.5 * m * v²"""
        return 0.5 * self.mass * (self.vx ** 2 + self.vy ** 2)


@dataclass
class CircleBody(Body):
    """Runder Körper mit Radius."""

    radius: float = 20.0

    def __post_init__(self):
        if self.mass == 1.0 and self.radius != 20.0:
            # Masse automatisch aus Fläche ableiten (Dichte ≈ 1)
            self.mass = self.radius ** 2 * math.pi * 0.01

    def contains_point(self, px: float, py: float) -> bool:
        """Punkt-Test: Liegt (px, py) innerhalb des Kreises?"""
        dx = px - self.x
        dy = py - self.y
        return dx * dx + dy * dy <= self.radius * self.radius


@dataclass
class RectBody(Body):
    """Rechteckiger Körper mit Breite und Höhe."""

    width: float = 40.0
    height: float = 40.0

    def __post_init__(self):
        if self.mass == 1.0 and self.width != 40.0:
            # Masse automatisch aus Fläche ableiten
            self.mass = self.width * self.height * 0.01

    def contains_point(self, px: float, py: float) -> bool:
        """Punkt-Test: Liegt (px, py) innerhalb des Rechtecks?"""
        half_w = self.width / 2
        half_h = self.height / 2
        return (self.x - half_w <= px <= self.x + half_w and
                self.y - half_h <= py <= self.y + half_h)
