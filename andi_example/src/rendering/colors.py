"""Farbdefinitionen für die Simulation."""

from typing import Tuple

Color = Tuple[int, int, int]


class Colors:
    """Zentrale Farbpalette."""

    # Basis
    WHITE: Color = (255, 255, 255)
    BLACK: Color = (0, 0, 0)
    GRAY: Color = (128, 128, 128)
    DARK_GRAY: Color = (40, 40, 50)
    LIGHT_GRAY: Color = (200, 200, 200)

    # Simulation
    BACKGROUND: Color = (20, 20, 30)
    GRID: Color = (40, 40, 50)
    FPS_TEXT: Color = (150, 255, 150)

    # Standard-Körper
    CORNFLOWER_BLUE: Color = (100, 149, 237)
    TOMATO: Color = (255, 99, 71)
    LIME_GREEN: Color = (50, 205, 50)
    GOLD: Color = (255, 215, 0)
    MAGENTA: Color = (255, 0, 255)
    CYAN: Color = (0, 255, 255)
    ORANGE: Color = (255, 165, 0)

    # Debug
    VELOCITY_VECTOR: Color = (255, 255, 0)
    FORCE_VECTOR: Color = (255, 0, 0)
    COLLISION_POINT: Color = (255, 255, 255)

    # UI
    BUTTON_BG: Color = (60, 60, 80)
    BUTTON_HOVER: Color = (80, 80, 110)
    BUTTON_TEXT: Color = (220, 220, 220)

    @classmethod
    def random_body_color(cls) -> Color:
        """Zufällige, gut sichtbare Körperfarbe."""
        import random
        bright_colors = [
            cls.CORNFLOWER_BLUE,
            cls.TOMATO,
            cls.LIME_GREEN,
            cls.GOLD,
            cls.CYAN,
            cls.ORANGE,
            cls.MAGENTA,
        ]
        return random.choice(bright_colors)
