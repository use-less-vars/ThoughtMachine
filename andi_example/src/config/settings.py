"""Globale Einstellungen für die Physiksimulation."""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class Settings:
    """Zentrale Konfiguration der Simulation."""

    # Fenster
    screen_width: int = 1280
    screen_height: int = 720
    fps: int = 60
    window_title: str = "Andi Physics Simulation"

    # Physik
    gravity: float = 9.81 * 50  # Pixel/s² (skaliert für Bildschirm)
    substeps: int = 8           # Physik-Substeps pro Frame
    damping: float = 0.999      # Geschwindigkeitsdämpfung (Luftwiderstand)

    # Rendering
    background_color: Tuple[int, int, int] = (20, 20, 30)
    show_fps: bool = True
    show_grid: bool = False
    grid_size: int = 50
    grid_color: Tuple[int, int, int] = (40, 40, 50)

    # Debug
    debug_mode: bool = False
    show_velocity_vectors: bool = False
    show_force_vectors: bool = False

    # Standard-Einstellungen
    default_body_color: Tuple[int, int, int] = (100, 149, 237)  # Cornflower Blue
    default_radius: float = 20.0
    default_mass: float = 1.0
    default_elasticity: float = 0.7  # 0 = komplett inelastisch, 1 = komplett elastisch
    default_friction: float = 0.3
