# Development Guides

Coding conventions, setup instructions, and development workflows.

## Current Status
- No guides recorded yet.

## Setup
(To be populated)

## Conventions
(To be populated)

## Workflows
(To be populated)

## 2026-06-10 — ## Coding-Konventionen

- **Python:** 3.10+ mit Type Hints
-...

## Coding-Konventionen

- **Python:** 3.10+ mit Type Hints
- **Code-Style:** PEP 8, `snake_case` für Funktionen/Variablen, `PascalCase` für Klassen
- **Dokumentation:** Google-Style Docstrings
- **Tests:** pytest, Dateien heißen `test_*.py`
- **Import-Reihenfolge:** Standardbibliothek → Drittanbieter → Eigenmodule

## Projektstruktur

```
andi_example/
├── src/
│   ├── main.py              # Einstiegspunkt
│   ├── physics/
│   │   ├── __init__.py
│   │   ├── engine.py        # Physik-Engine
│   │   ├── body.py          # Physikalische Körper
│   │   ├── forces.py        # Kraft-Typen
│   │   └── collision.py     # Kollisionserkennung
│   ├── rendering/
│   │   ├── __init__.py
│   │   ├── renderer.py      # Pygame-Renderer
│   │   └── colors.py        # Farben
│   └── config/
│       ├── __init__.py
│       └── settings.py      # Einstellungen
├── requirements.txt
├── README.md
└── .gitignore
```
