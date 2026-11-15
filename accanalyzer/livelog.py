"""Live-Mitschnitt aus dem ACC Shared Memory, während das Fenster offen ist.

Das MoTeC-File enthält weder Luft-/Asphalttemperatur noch Session-Art oder -Länge. Deshalb schreibt das Fenster
alle paar Sekunden eine Zeile in  Dokumente\\ACC-Analyse\\live\\live_JJJJ-MM-TT.jsonl:
    Uhrzeit, Session-Art (Training/Quali/Rennen), Restzeit, Luft, Asphalt, Auto, Strecke, Sprit.
Bei der Auswertung werden diese Zeilen über die Uhrzeit den Sessions der .ld-Aufnahme zugeordnet.

Außerdem puffert der Thread die laufende Session mit ca. 25 Hz für die Live-Auswertung (siehe livesession.py).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# ACC_SESSION_TYPE aus der ACC Shared-Memory-Doku
SESSION_TYPES = {0: "Training", 1: "Quali", 2: "Rennen", 3: "Hotlap", 4: "Time Attack", 7: "Hotstint", 8: "Superpole"}
MODE_OF_TYPE = {1: "quali", 8: "quali", 2: "race"}


def docs_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for cand in (home / "Documents", home / "Dokumente", home / "OneDrive" / "Documents", home / "OneDrive" / "Dokumente"):
        if (cand / "Assetto Corsa Competizione").exists():
            return cand
    return home / "Documents"


def live_dir() -> Path:
    return docs_dir() / "ACC-Analyse" / "live"


class LiveLogger(threading.Thread):
    """Hintergrund-Thread: liest ACC mit `hz` Hz in den Live-Puffer (recorder) und schreibt alle `interval` s eine
    Zeile in den Mitschnitt (Luft, Asphalt, Session-Art, Restzeit, Sprit)."""

    FROZEN_S = 30.0   # so lange keine neuen Daten -> ACC beendet/Pause: Fläche freigeben und neu warten

    def __init__(self, folder: Path | None = None, interval: float = 5.0, hz: float = 25.0):
        super().__init__(daemon=True)
        from .livesession import Recorder
        self.folder = Path(folder or live_dir())
        self.interval = interval
        self.hz = hz
        self.recorder = Recorder()
        from .livebalance import BalanceMonitor, load_settings
        self.monitor = BalanceMonitor(load_settings())
        self.state = "Live-Mitschnitt: warte auf ACC …"
        self.count = 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        self.monitor.stop()

    def run(self):
        from .shm import ACCReader, wstr
        reader, last_ids, stale = None, None, None
        self.monitor.start_sound()
        changed_at, written_at, state_at = time.time(), 0.0, 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if reader is None:
                    reader = ACCReader()
                    last_ids, changed_at = None, now
                p, g, s = reader.read()
                ids = (p.packetId, g.packetId)
                fresh = ids != last_ids
                if fresh:
                    changed_at = now
                last_ids = ids
                if ids == stale:         # immer noch derselbe eingefrorene Stand -> nichts schreiben
                    self.state = "Live-Mitschnitt: ACC inaktiv – warte …"
                elif now - changed_at > self.FROZEN_S:   # nichts mehr aktualisiert -> Fläche freigeben, neu warten
                    stale = ids
                    reader.close()
                    reader = None
                    self.state = "Live-Mitschnitt: ACC inaktiv – warte …"
                elif g.status == 2 and fresh:   # 2 = AC_LIVE (nicht Replay/Pause)
                    car, track = wstr(s.carModel), wstr(s.track)
                    self.recorder.add(p, g, s, now, car, track)
                    self.monitor.update(p, g, now)
                    if now - written_at >= self.interval:
                        written_at = now
                        rec = dict(ts=round(now, 1), session=int(g.session), left=round(g.sessionTimeLeft / 1000.0, 1),
                                   air=round(p.airTemp, 1), road=round(p.roadTemp, 1), car=car, track=track,
                                   fuel=round(p.fuel, 1), pit=int(g.isInPit))
                        self._write(rec)
                    if now - state_at >= 1.0:
                        state_at = now
                        left = g.sessionTimeLeft / 60000.0
                        lt = f", noch {left:.0f} min" if left > 0 else ""
                        self.state = (f"● Live: {SESSION_TYPES.get(int(g.session), '?')}{lt} · {self.recorder.laps_done} Runden · "
                                      f"Luft {p.airTemp:.0f} °C · Asphalt {p.roadTemp:.0f} °C")
                elif g.status != 2:
                    self.state = "Live-Mitschnitt: ACC läuft – warte auf Session …"
            except RuntimeError:
                reader = None
                self.state = "Live-Mitschnitt: warte auf ACC …"
            except Exception as e:  # nie das Fenster abschießen
                if reader is not None:
                    reader.close()
                reader = None
                self.state = f"Live-Mitschnitt: Fehler ({e})"
            self._stop.wait(1.0 / self.hz if reader is not None else 2.0)
        if reader is not None:
            reader.close()

    def _write(self, rec: dict):
        self.folder.mkdir(parents=True, exist_ok=True)
        f = self.folder / f"live_{datetime.fromtimestamp(rec['ts']):%Y-%m-%d}.jsonl"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        self.count += 1


def load_samples(folder: Path, t0: float, t1: float) -> list[dict]:
    """Alle Mitschnitt-Zeilen im Zeitraum [t0, t1] (Unix-Zeit)."""
    folder = Path(folder)
    out = []
    d = datetime.fromtimestamp(t0).date() - timedelta(days=1)
    while d <= datetime.fromtimestamp(t1).date():
        f = folder / f"live_{d:%Y-%m-%d}.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if t0 <= r.get("ts", 0) <= t1:
                    out.append(r)
        d += timedelta(days=1)
    return sorted(out, key=lambda r: r["ts"])


def attach(meta: dict, folder: Path | None = None) -> int:
    """Hängt an jede Session der Aufnahme s["live"] = {type, air, road, minutes, n} an (falls Mitschnitt vorhanden).
    Session-Art aus ACC ersetzt die Erkennung. Rückgabe: Anzahl Sessions mit Live-Daten."""
    rec = meta.get("rec_start")
    ss = meta.get("sessions") or []
    if not rec or not ss:
        return 0
    folder = Path(folder or live_dir())
    samples = load_samples(folder, rec + ss[0]["start"] - 3600, rec + ss[-1]["end"] + 120)
    track = str(meta.get("track", "")).lower()
    if track:
        samples = [r for r in samples if not r.get("track") or _same_track(r["track"], track)]
    n = 0
    prev_end = -1e18
    for s in ss:
        a0, a1 = rec + s["start"], rec + s["end"]
        inside = [r for r in samples if a0 - 5 <= r["ts"] <= a1 + 5]
        if not inside:
            prev_end = a1
            continue
        typ = statistics.mode(r["session"] for r in inside)
        # Restzeit: auch die Zeilen vor dem Losfahren (Box/Grid) zählen, solange es dieselbe Session-Art ist
        before = [r for r in samples if max(prev_end, a0 - 1800) < r["ts"] < a0 and r["session"] == typ]
        lefts = [r["left"] for r in before + inside if r["session"] == typ and r["left"] > 0]
        minutes = int(math.ceil(max(lefts) / 60.0 - 0.05)) if lefts else None
        s["live"] = dict(type=typ, type_name=SESSION_TYPES.get(typ, "?"), n=len(inside),
                         air=statistics.median(r["air"] for r in inside), road=statistics.median(r["road"] for r in inside),
                         minutes=minutes, fuel_start=inside[0].get("fuel"), fuel_end=inside[-1].get("fuel"))
        if typ in (0, 1, 2):
            s["type"] = {0: "Training", 1: "Quali", 2: "Rennen"}[typ]
        n += 1
        prev_end = a1
    meta["live_sessions"] = n
    return n


def _same_track(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or a.startswith(b) or b.startswith(a)
