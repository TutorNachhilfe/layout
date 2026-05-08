# layout

Lokales LAN-Quiz-System (ohne Internet), vorbereitet für spätere Zusammenführung mit **gnome-classshare**.

## Datei
- `/home/runner/work/layout/layout/layout.py` – GTK4-Lehrerfenster + HTTP/WebSocket-Server + Browser-Client

## Start
```bash
python3 /home/runner/work/layout/layout/layout.py
```

## Abhängigkeiten
- `gi` (GTK4 + Libadwaita)
- `websockets` (optional, bei Fehlen wird HTTP-Polling genutzt)

## Enthaltene Architektur (kompatibler Stil)
- `LayoutState`
- `LayoutHandler`
- `LayoutWindow`
- `LayoutApp`

## Features
- Lehrer-Tabs: Aufgaben, Zufallsgenerator, Aufzeigen, Mehr-Zeit, Ranking
- Schülerseite: Name (localStorage), Aufgabe + Timer, Antwortfeld + Abgeben, Aufzeigen, Mehr Zeit
- Wartebildschirm: Einmaleins-Training + Ranking (nur ab mindestens 10 Aufgaben)
- Alles nur im RAM (kein Speichern von Quizdaten, bei Neustart leer)
- Fallback ohne WebSocket: `GET /state` alle 2 Sekunden

## Konfiguration
- Settings unter `~/.config/layout/settings.json`
- `.desktop`-Datei unter `~/.local/share/applications/layout.desktop`
