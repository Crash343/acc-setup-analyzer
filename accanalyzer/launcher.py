"""Einstiegspunkt für die .exe (und `python -m accanalyzer`): öffnet das Fenster.

  * Doppelklick -> Fenster, neueste Aufnahme aus dem MoTeC-Ordner ist vorausgewählt
  * .ld (und optional Setup-.json) bzw. einen Ordner auf die .exe ziehen -> Fenster mit diesen Dateien, Analyse startet sofort
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _newest_ld(folder: Path) -> Path | None:
    files = sorted(folder.glob("*.ld"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run_gui(argv: list[str]) -> int:
    paths = [Path(a.strip('"')) for a in argv]
    ld = next((p for p in paths if p.suffix.lower() == ".ld"), None)
    if ld is None:   # Ordner gezogen -> neueste .ld darin
        ld = next((n for p in paths if p.is_dir() for n in [_newest_ld(p)] if n), None)
    setup = next((p for p in paths if p.suffix.lower() == ".json"), None)
    from .gui import main as gui_main
    gui_main(str(ld) if ld else None, str(setup) if setup else None)
    return 0


def _error_box(text: str):
    """Die .exe hat keine Konsole – Fehler beim Start als Meldungsfenster zeigen."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text[-1500:], "ACC-Analyzer", 0x10)
    except Exception:
        print(text, file=sys.stderr)


def main():
    try:
        sys.exit(run_gui(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        _error_box("ACC-Analyzer konnte nicht gestartet werden.\n\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
