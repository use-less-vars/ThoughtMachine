# System Architecture

Key architectural decisions, component relationships, and data flow patterns.

## Current Status
- No architecture notes recorded yet.

## Components
(To be populated)

## Data Flow
(To be populated)

## 2026-06-10 — ## Projektübersicht

**Projekt:** 2D-Physiksimulation mit Py...

## Projektübersicht

**Projekt:** 2D-Physiksimulation mit Pygame
**Start:** 2026-06-10
**Sprache:** Python 3.10+
**Abhängigkeiten:** Pygame, numpy (optional für Vektorrechnung)

### Architektur (Schichtenmodell)

```
┌─────────────────────────────┐
│      main.py (Einstieg)     │
├─────────────────────────────┤
│    rendering/ (Pygame-GUI)  │
├─────────────────────────────┤
│    physics/ (Simulations-   │
│              Kern)          │
├─────────────────────────────┤
│    config/ (Einstellungen)  │
└─────────────────────────────┘
```

### Kernkonzepte

1. **Physik-Engine** – verwaltet alle physikalischen Körper, wendet Kräfte an, löst Kollisionen
2. **Physikalische Körper** – haben Position, Geschwindigkeit, Masse, Form (Kreis, Rechteck)
3. **Kräfte** – Gravitation, Federkraft, Reibung, Wind
4. **Kollisionserkennung** – Kreis-Kreis, Kreis-Rechteck, Rechteck-Rechteck (SAT)
5. **Renderer** – zeichnet Körper mit Pygame, optional mit Trails, Grid, Debug-Overlay
6. **Config** – Bildschirmgröße, Farben, Physik-Parameter
