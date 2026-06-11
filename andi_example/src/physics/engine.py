"""Physik-Engine – Herzstück der Simulation.

Verwaltet alle Körper und Kräfte, führt Zeitschritte aus.
"""

from __future__ import annotations

from typing import List, Callable, Optional
from .body import Body
from .forces import Force, Gravity, drag_force
from .collision import CollisionSolver


class PhysicsEngine:
    """Haupt-Engine der Physiksimulation.

    Koordiniert:
    - Hinzufügen/Entfernen von Körpern
    - Anwenden von Kräften
    - Zeitschritt-Integration (Semi-impliziter Euler mit Substepping)
    - Kollisionserkennung und -auflösung
    """

    def __init__(self, gravity: float = 9.81 * 50, substeps: int = 8):
        self.bodies: List[Body] = []
        self.forces: List[Force] = []
        self.custom_force_fns: List[Callable] = []

        # Standard-Kraft: Gravitation
        self.forces.append(Gravity(gravity))

        # Kollisionslöser
        self.collision_solver = CollisionSolver()

        # Einstellungen
        self.substeps = substeps
        self.damping = 0.999

    def add_body(self, body: Body) -> Body:
        """Füge einen Körper zur Simulation hinzu."""
        self.bodies.append(body)
        return body

    def remove_body(self, body: Body) -> None:
        """Entferne einen Körper aus der Simulation."""
        if body in self.bodies:
            self.bodies.remove(body)

    def add_force(self, force: Force) -> None:
        """Füge eine Kraft zur Simulation hinzu."""
        self.forces.append(force)

    def add_custom_force(self, fn: Callable) -> None:
        """Füge eine benutzerdefinierte Kraft-Funktion hinzu.

        Die Funktion muss die Signatur (body: Body, dt: float) -> (fx, fy) haben.
        """
        self.custom_force_fns.append(fn)

    def clear_forces(self) -> None:
        """Entferne alle Kräfte (inkl. Gravitation)."""
        self.forces.clear()
        self.custom_force_fns.clear()

    def step(self, dt: float) -> None:
        """Führe einen vollständigen Physik-Schritt aus.

        Verwendet Substepping für stabilere Simulation.
        """
        sub_dt = dt / self.substeps

        for _ in range(self.substeps):
            self._apply_forces(sub_dt)
            self._integrate(sub_dt)
            self._solve_collisions(sub_dt)

    def _apply_forces(self, dt: float) -> None:
        """Wende alle registrierten Kräfte auf alle Körper an."""
        for body in self.bodies:
            if body.static or not body.active:
                continue

            # Registrierte Forces
            for force in self.forces:
                fx, fy = force(body, dt)
                body.apply_force(fx, fy)

            # Benutzerdefinierte Forces
            for fn in self.custom_force_fns:
                fx, fy = fn(body, dt)
                body.apply_force(fx, fy)

            # Luftwiderstand
            fx, fy = drag_force(body, dt)
            body.apply_force(fx, fy)

    def _integrate(self, dt: float) -> None:
        """Führe die Integration für alle Körper durch."""
        for body in self.bodies:
            body.update(dt)
            # Dämpfung
            body.vx *= self.damping
            body.vy *= self.damping

    def _solve_collisions(self, dt: float) -> None:
        """Erkenne und löse Kollisionen."""
        self.collision_solver.detect_and_resolve(self.bodies, dt)

    def get_bodies_at(self, x: float, y: float) -> List[Body]:
        """Finde alle Körper, die den Punkt (x, y) enthalten."""
        return [b for b in self.bodies if hasattr(b, 'contains_point')
                and b.contains_point(x, y) and b.active]

    def reset(self) -> None:
        """Setze die Simulation zurück (entferne alle Körper)."""
        self.bodies.clear()
