# Andi Physics Simulation

Eine 2D-Physiksimulation gebaut mit **Python** und **Pygame**.

## Installation

```bash
pip install -r requirements.txt
```

## Ausführen

```bash
python -m src.main
```

## Projektstruktur

```
andi_example/
├── src/
│   ├── main.py              # Einstiegspunkt
│   ├── physics/             # Physik-Engine
│   ├── rendering/           # Pygame-Renderer
│   └── config/              # Einstellungen
├── requirements.txt
└── README.md
```

## Features (geplant)

- [x] Gravitation & Bewegung
- [ ] Kollisionserkennung (Kreis-Kreis, Kreis-Rechteck)
- [ ] Interaktive Maus-Steuerung
- [ ] Mehrere Kraft-Typen (Feder, Reibung, Wind)
- [ ] Partikel-System
- [ ] Szenen-Manager
