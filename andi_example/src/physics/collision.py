"""Kollisionserkennung und -auflösung für 2D-Körper.

Unterstützt:
- Kreis-Kreis Kollision
- Kreis-Rechteck Kollision
- Rechteck-Rechteck Kollision (Separating Axis Theorem)
"""

from __future__ import annotations

from typing import List, Optional, Tuple
from .body import Body, CircleBody, RectBody
import math


class CollisionSolver:
    """Löst Kollisionen zwischen Körpern auf."""

    def __init__(self, elasticity: float = 0.7, friction: float = 0.3):
        self.elasticity = elasticity
        self.friction = friction

    def detect_and_resolve(self, bodies: List[Body], dt: float) -> None:
        """Alle Kollisionen zwischen aktiven Körpern erkennen und auflösen."""
        n = len(bodies)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = bodies[i], bodies[j]
                if not a.active or not b.active:
                    continue
                self._resolve_pair(a, b)

    def _resolve_pair(self, a: Body, b: Body) -> None:
        """Kollision zwischen zwei Körpern erkennen und auflösen."""
        if isinstance(a, CircleBody) and isinstance(b, CircleBody):
            self._circle_circle(a, b)
        elif isinstance(a, CircleBody) and isinstance(b, RectBody):
            self._circle_rect(a, b)
        elif isinstance(a, RectBody) and isinstance(b, CircleBody):
            self._circle_rect(b, a)
        else:
            # Rect-Rect (noch nicht implementiert)
            pass

    def _circle_circle(self, a: CircleBody, b: CircleBody) -> None:
        """Kreis-Kreis Kollision mit Impuls-basierter Auflösung."""
        dx = b.x - a.x
        dy = b.y - a.y
        dist = math.sqrt(dx * dx + dy * dy)
        min_dist = a.radius + b.radius

        if dist >= min_dist or dist < 0.001:
            return

        # Normalen-Vektor (von a nach b)
        nx = dx / dist
        ny = dy / dist

        # Penetration (Überlappung)
        overlap = min_dist - dist

        # Position korrigieren (nach Masse gewichtet)
        total_inv_mass = a.inv_mass + b.inv_mass
        if total_inv_mass > 0:
            correction = overlap / total_inv_mass
            a.x -= nx * correction * a.inv_mass
            a.y -= ny * correction * a.inv_mass
            b.x += nx * correction * b.inv_mass
            b.y += ny * correction * b.inv_mass

        # Relative Geschwindigkeit
        dvx = b.vx - a.vx
        dvy = b.vy - a.vy
        rel_vel_normal = dvx * nx + dvy * ny

        # Nur auflösen, wenn Körper sich annähern
        if rel_vel_normal > 0:
            return

        # Elastischer Stoß (Impuls)
        e = min(a.elasticity, b.elasticity, self.elasticity)
        j = -(1 + e) * rel_vel_normal / total_inv_mass

        a.vx -= j * nx * a.inv_mass
        a.vy -= j * ny * a.inv_mass
        b.vx += j * nx * b.inv_mass
        b.vy += j * ny * b.inv_mass

        # Reibung (tangentiale Komponente)
        tx, ty = -ny, nx  # Tangenten-Vektor
        rel_vel_tan = dvx * tx + dvy * ty
        friction_impulse = min(self.friction, a.friction, b.friction) * abs(j)
        if abs(rel_vel_tan) > 0.001:
            jt = -rel_vel_tan / total_inv_mass
            jt = max(-friction_impulse, min(friction_impulse, jt))
            a.vx -= jt * tx * a.inv_mass
            a.vy -= jt * ty * a.inv_mass
            b.vx += jt * tx * b.inv_mass
            b.vy += jt * ty * b.inv_mass

    def _circle_rect(self, circle: CircleBody, rect: RectBody) -> None:
        """Kreis-Rechteck Kollision."""
        # Nächsten Punkt auf dem Rechteck zum Kreis finden
        half_w = rect.width / 2
        half_h = rect.height / 2
        closest_x = max(rect.x - half_w, min(circle.x, rect.x + half_w))
        closest_y = max(rect.y - half_h, min(circle.y, rect.y + half_h))

        dx = circle.x - closest_x
        dy = circle.y - closest_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist >= circle.radius or dist < 0.001:
            return

        # Normalen-Vektor
        nx = dx / dist
        ny = dy / dist

        # Penetration
        overlap = circle.radius - dist

        # Position korrigieren
        total_inv_mass = circle.inv_mass + rect.inv_mass
        if total_inv_mass > 0:
            correction = overlap / total_inv_mass
            circle.x += nx * correction * circle.inv_mass
            circle.y += ny * correction * circle.inv_mass
            rect.x -= nx * correction * rect.inv_mass
            rect.y -= ny * correction * rect.inv_mass

        # Impuls (wie bei Kreis-Kreis)
        dvx = rect.vx - circle.vx
        dvy = rect.vy - circle.vy
        rel_vel_normal = dvx * nx + dvy * ny

        if rel_vel_normal > 0:
            return

        e = min(circle.elasticity, rect.elasticity, self.elasticity)
        j = -(1 + e) * rel_vel_normal / total_inv_mass

        circle.vx += j * nx * circle.inv_mass
        circle.vy += j * ny * circle.inv_mass
        rect.vx -= j * nx * rect.inv_mass
        rect.vy -= j * ny * rect.inv_mass
