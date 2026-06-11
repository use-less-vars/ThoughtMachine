"""Pygame-Renderer – Zeichnet die Simulation auf den Bildschirm."""

from __future__ import annotations

from typing import List, Optional, Tuple
import pygame

from .colors import Colors, Color
from ..physics import Body, CircleBody, RectBody
from ..config import Settings


class Renderer:
    """Zeichnet die Physik-Simulation mit Pygame."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.screen: Optional[pygame.Surface] = None
        self.clock: Optional[pygame.time.Clock] = None
        self.font: Optional[pygame.font.Font] = None
        self.small_font: Optional[pygame.font.Font] = None
        self.running = False

    def initialize(self) -> None:
        """Initialisiert Pygame und das Fenster."""
        pygame.init()
        pygame.display.set_caption(self.settings.window_title)

        self.screen = pygame.display.set_mode(
            (self.settings.screen_width, self.settings.screen_height)
        )
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("monospace", 20)
        self.small_font = pygame.font.SysFont("monospace", 14)
        self.running = True

    def handle_events(self) -> None:
        """Verarbeitet Pygame-Events (Fenster schließen, Tastatur)."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_g:
                    self.settings.show_grid = not self.settings.show_grid
                elif event.key == pygame.K_d:
                    self.settings.debug_mode = not self.settings.debug_mode
                elif event.key == pygame.K_v:
                    self.settings.show_velocity_vectors = not self.settings.show_velocity_vectors
                elif event.key == pygame.K_f:
                    self.settings.show_force_vectors = not self.settings.show_force_vectors

    def clear(self) -> None:
        """Füllt den Bildschirm mit der Hintergrundfarbe."""
        self.screen.fill(self.settings.background_color)

    def draw_grid(self) -> None:
        """Zeichnet ein Hintergrundraster."""
        if not self.settings.show_grid:
            return

        w, h = self.settings.screen_width, self.settings.screen_height
        gs = self.settings.grid_size

        for x in range(0, w, gs):
            pygame.draw.line(self.screen, self.settings.grid_color,
                             (x, 0), (x, h), 1)
        for y in range(0, h, gs):
            pygame.draw.line(self.screen, self.settings.grid_color,
                             (0, y), (w, y), 1)

    def draw_body(self, body: Body) -> None:
        """Zeichnet einen einzelnen physikalischen Körper."""
        if not body.active:
            return

        if isinstance(body, CircleBody):
            pygame.draw.circle(
                self.screen, body.color,
                (int(body.x), int(body.y)),
                int(body.radius)
            )
        elif isinstance(body, RectBody):
            rect = pygame.Rect(
                int(body.x - body.width / 2),
                int(body.y - body.height / 2),
                int(body.width),
                int(body.height)
            )
            pygame.draw.rect(self.screen, body.color, rect)

        # Label zeichnen
        if body.label and self.small_font:
            label_surf = self.small_font.render(body.label, True, Colors.WHITE)
            self.screen.blit(label_surf, (int(body.x) + 5, int(body.y) - 15))

    def draw_bodies(self, bodies: List[Body]) -> None:
        """Zeichnet alle Körper."""
        for body in bodies:
            self.draw_body(body)

    def draw_debug_vectors(self, bodies: List[Body]) -> None:
        """Zeichnet Debug-Vektoren (Geschwindigkeit, Kräfte)."""
        for body in bodies:
            if not body.active or body.static:
                continue

            # Geschwindigkeits-Vektor
            if self.settings.show_velocity_vectors:
                end_x = int(body.x + body.vx * 0.5)
                end_y = int(body.y + body.vy * 0.5)
                pygame.draw.line(
                    self.screen, Colors.VELOCITY_VECTOR,
                    (int(body.x), int(body.y)), (end_x, end_y), 2
                )

    def draw_fps(self, fps: float) -> None:
        """Zeichnet die aktuelle FPS-Anzeige."""
        if not self.settings.show_fps or not self.font:
            return
        fps_text = self.font.render(f"FPS: {fps:.1f}", True, Colors.FPS_TEXT)
        self.screen.blit(fps_text, (10, 10))

    def draw_info(self, text: str, y_offset: int = 30) -> None:
        """Zeichnet eine Info-Zeile auf den Bildschirm."""
        if not self.small_font:
            return
        info_surf = self.small_font.render(text, True, Colors.LIGHT_GRAY)
        self.screen.blit(info_surf, (10, y_offset))

    def draw_help(self) -> None:
        """Zeichnet Hilfe-Text (Tastatur-Kürzel)."""
        if not self.small_font:
            return
        help_lines = [
            "ESC - Beenden",
            "G   - Grid umschalten",
            "D   - Debug-Modus",
            "V   - Geschwindigkeits-Vektoren",
        ]
        y = self.settings.screen_height - 100
        for line in help_lines:
            surf = self.small_font.render(line, True, Colors.DARK_GRAY)
            self.screen.blit(surf, (10, y))
            y += 18

    def flip(self) -> None:
        """Aktualisiert den Bildschirm (double buffer)."""
        pygame.display.flip()

    def tick(self) -> float:
        """Wartet auf den nächsten Frame und gibt die vergangene Zeit zurück."""
        if self.clock:
            return self.clock.tick(self.settings.fps) / 1000.0
        return 0.016

    def shutdown(self) -> None:
        """Räumt Pygame-Ressourcen auf."""
        self.running = False
        pygame.quit()
