"""Andi Physics Simulation – Haupt-Einstiegspunkt.

Startet die Physiksimulation mit einer Demo-Szene.
"""

from __future__ import annotations

import sys
from typing import List

from .config import Settings
from .physics import PhysicsEngine, CircleBody, RectBody
from .rendering import Renderer, Colors


def demo_falling_balls(engine: PhysicsEngine) -> List[CircleBody]:
    """Erzeugt eine Demo-Szene mit fallenden Bällen."""
    bodies = []
    for i in range(5):
        ball = CircleBody(
            x=200 + i * 100,
            y=100 + i * 20,
            vx=0,
            vy=0,
            radius=20 + i * 5,
            color=Colors.random_body_color(),
            label=f"Ball {i + 1}",
        )
        engine.add_body(ball)
        bodies.append(ball)
    return bodies


def demo_bouncing_balls(engine: PhysicsEngine) -> List[CircleBody]:
    """Erzeugt eine Demo mit hüpfenden Bällen."""
    bodies = []

    # Boden (statisches Rechteck)
    floor = RectBody(
        x=400,
        y=680,
        width=760,
        height=30,
        color=Colors.DARK_GRAY,
        static=True,
        elasticity=0.8,
        label="Boden",
    )
    engine.add_body(floor)
    bodies.append(floor)

    # Wände (statisch)
    left_wall = RectBody(
        x=20, y=360, width=20, height=720,
        color=Colors.DARK_GRAY, static=True, elasticity=0.8,
    )
    right_wall = RectBody(
        x=780, y=360, width=20, height=720,
        color=Colors.DARK_GRAY, static=True, elasticity=0.8,
    )
    engine.add_body(left_wall)
    engine.add_body(right_wall)
    bodies.extend([left_wall, right_wall])

    # Bälle
    for i in range(3):
        ball = CircleBody(
            x=200 + i * 200,
            y=100,
            vy=50,
            radius=25,
            color=Colors.random_body_color(),
            elasticity=0.85,
            label=f"Ball {i + 1}",
        )
        engine.add_body(ball)
        bodies.append(ball)

    return bodies


def main() -> None:
    """Hauptfunktion – startet die Simulation."""
    settings = Settings()
    engine = PhysicsEngine(gravity=settings.gravity, substeps=settings.substeps)
    renderer = Renderer(settings)

    # Demo-Szene laden
    bodies = demo_bouncing_balls(engine)

    # Renderer initialisieren
    renderer.initialize()

    # Hauptschleife
    while renderer.running:
        # Events verarbeiten
        renderer.handle_events()

        # Maus-Interaktion (optional)
        # TODO: Maus-greifen von Körpern

        # Physik-Schritt
        dt = renderer.tick()
        engine.step(dt)

        # Rendern
        renderer.clear()
        renderer.draw_grid()
        renderer.draw_bodies(engine.bodies)
        renderer.draw_debug_vectors(engine.bodies)

        # Overlay
        renderer.draw_fps(renderer.clock.get_fps() if renderer.clock else 0)
        renderer.draw_info(f"Körper: {len(engine.bodies)}", y_offset=30)
        renderer.draw_info(f"Substeps: {settings.substeps}", y_offset=48)
        renderer.draw_help()

        renderer.flip()

    # Aufräumen
    renderer.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
