"""Unter-/Übersteuern in Echtzeit mit Warnton – ähnlich wie bei ACC Race Engineer.

Aufbau (wie dort): eigene Schwellen für Unter- und Übersteuern, eine Geschwindigkeitsgrenze trennt langsame von schnellen
Kurven, gezählt wird je Phase (Bremsen / Rollen / Gas). Die genaue Formel von Race Engineer ist nicht veröffentlicht –
hier zählt der Schräglaufwinkel aus dem ACC Shared Memory:

    d = Ø |Schräglauf vorne| − Ø |Schräglauf hinten|   (Grad, leicht geglättet)
    d >  us_deg           -> Untersteuern (Vorderachse rutscht mehr)
    d < −os_deg  oder Gegenlenken -> Übersteuern (Heck rutscht mehr)

Ton: tief = Untersteuern, hoch = Übersteuern. Je stärker, desto schneller folgen die Töne (wie eine Einparkhilfe).
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

PHASES = ("brake", "coast", "accel")
PHASE_DE = {"brake": "Bremsen", "coast": "Rollen", "accel": "Gas"}
DEFAULTS = dict(sound=True, us_deg=1.5, os_deg=0.75, slow_kmh=100, tone_us=440, tone_os=1400)
MIN_G = 0.5          # g – darunter keine Kurve (Geradeaus, Bodenwellen)
MIN_KMH = 40
SPAN = 3.0           # Grad über der Schwelle -> maximale Tonfolge
GAP_SLOW, GAP_FAST = 0.7, 0.12   # s zwischen zwei Tönen: knapp über der Schwelle … stark
BEEP_MS = 70
HOLD_S = 0.15        # so lange muss der Zustand anliegen, bevor es piept (kein Piepen bei einzelnen Ausreißern)


def settings_path() -> Path:
    from .livelog import docs_dir
    return docs_dir() / "ACC-Analyse" / "einstellungen.json"


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update({k: v for k, v in json.loads(settings_path().read_text(encoding="utf-8")).get("balance", {}).items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return s


def save_settings(s: dict):
    p = settings_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    data["balance"] = {k: s[k] for k in DEFAULTS if k in s}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _beep(freq: int, ms: int):
    try:
        import winsound
        winsound.Beep(int(freq), int(ms))
    except Exception:   # kein Windows / kein Audiogerät
        time.sleep(ms / 1000.0)


class BalanceMonitor:
    """update() bekommt jedes Live-Sample (aus dem Mitschnitt-Thread); ein eigener Thread spielt die Töne."""

    def __init__(self, settings: dict | None = None, beep=_beep):
        self.s = dict(DEFAULTS, **(settings or {}))
        self._beep = beep
        self._lock = threading.Lock()
        self.reset_counts()
        self.kind = None          # "us" | "os" | None (aktueller Zustand)
        self.severity = 0.0       # 0..1
        self.value = 0.0          # geglättetes d in Grad
        self.updated = 0.0
        self._d = None
        self._since = None
        self._rad = None          # Einheit des Schräglaufs (ACC: Radiant) – wird aus den ersten Kurven bestimmt
        self._unit_samples: list[float] = []
        self._sign_votes = 0      # Lenk-/Gier-Vorzeichen bei normaler Kurvenfahrt (für Gegenlenken)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_events: list[str] = []

    # ------------------------------------------------------------------ Zähler
    def reset_counts(self):
        with getattr(self, "_lock", threading.Lock()):
            self.counts = {(spd, kind): {p: 0 for p in PHASES} for spd in ("slow", "fast") for kind in ("us", "os")}
            self._episode = None

    def counts_text(self, spd: str, kind: str) -> str:
        c = self.counts[(spd, kind)]
        return " / ".join(str(c[p]) for p in PHASES)

    # ------------------------------------------------------------------ Messung
    def update(self, p, g, now: float):
        speed = float(p.speedKmh)
        glat = abs(float(p.accG[0]))
        cornering = speed > MIN_KMH and glat > MIN_G and not (g.isInPit or getattr(g, "isInPitLane", 0))
        if not cornering:
            self._set(None, 0.0, now)
            self._d = None
            return
        sa = [abs(float(p.slipAngle[i])) for i in range(4)]
        if self._rad is None:
            self._unit_samples.append(max(sa))
            if len(self._unit_samples) >= 50:
                self._unit_samples.sort()
                self._rad = self._unit_samples[len(self._unit_samples) // 2] < 0.2
            return
        if self._rad:
            sa = [math.degrees(x) for x in sa]
        d = (sa[0] + sa[1]) / 2 - (sa[2] + sa[3]) / 2
        self._d = d if self._d is None else 0.7 * self._d + 0.3 * d     # ~0,1 s Glättung bei 25 Hz
        steer, yaw = float(p.steerAngle), float(p.localAngularVel[1])
        counter = False
        if abs(steer) > 0.01 and abs(yaw) > 0.05:
            same = (steer > 0) == (yaw > 0)
            if glat > 1.0 and abs(self._d) < self.s["us_deg"]:          # normale Kurvenfahrt: Vorzeichen lernen
                self._sign_votes = max(-200, min(200, self._sign_votes + (1 if same else -1)))
            if abs(self._sign_votes) > 50 and abs(yaw) > 0.2:
                counter = same != (self._sign_votes > 0)
        us, os_ = self.s["us_deg"], self.s["os_deg"]
        if self._d > us:
            kind, sev = "us", (self._d - us) / SPAN
        elif self._d < -os_ or counter:
            kind, sev = "os", max((-self._d - os_) / SPAN, 0.5 if counter else 0.0)
        else:
            kind, sev = None, 0.0
        phase = "brake" if p.brake > 0.1 else "accel" if p.gas > 0.3 else "coast"
        self._set(kind, min(1.0, max(0.0, sev)), now, phase, "slow" if speed < self.s["slow_kmh"] else "fast")
        self.value = self._d

    def _set(self, kind, sev, now, phase=None, spd=None):
        with self._lock:
            if kind != self.kind:
                self._since = now
            self.kind, self.severity, self.updated = kind, sev, now
            held = kind is not None and now - (self._since or now) >= HOLD_S
            if held and self._episode != kind:           # neue Episode zählen
                self.counts[(spd, kind)][phase] += 1
                self.last_events = (self.last_events + [f"{time.strftime('%H:%M:%S')} {'Untersteuern' if kind == 'us' else 'Übersteuern'} "
                                                        f"({'langsam' if spd == 'slow' else 'schnell'}, {PHASE_DE[phase]})"])[-8:]
            if kind is None or held:
                self._episode = kind if held else None

    def active(self, now: float) -> tuple[str | None, float]:
        """Aktueller Zustand für den Ton (veraltet nach 0,3 s -> nichts)."""
        with self._lock:
            if self.kind is None or now - self.updated > 0.3 or now - (self._since or now) < HOLD_S:
                return None, 0.0
            return self.kind, self.severity

    # ------------------------------------------------------------------ Ton
    @staticmethod
    def gap(sev: float) -> float:
        """Abstand zwischen zwei Tönen: je stärker, desto kürzer."""
        return GAP_SLOW - (GAP_SLOW - GAP_FAST) * min(1.0, max(0.0, sev))

    def start_sound(self):
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._sound_loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def _sound_loop(self):
        next_at = 0.0
        while not self._stop.is_set():
            now = time.time()
            kind, sev = self.active(now)
            if kind and self.s.get("sound") and now >= next_at:
                self._beep(self.s["tone_us"] if kind == "us" else self.s["tone_os"], BEEP_MS)
                next_at = time.time() + self.gap(sev)
            elif not kind:
                next_at = 0.0
            self._stop.wait(0.02)

    def test_beep(self, kind: str):
        threading.Thread(target=lambda: [self._beep(self.s["tone_us" if kind == "us" else "tone_os"], BEEP_MS)
                                         or time.sleep(self.gap(s) - BEEP_MS / 1000) for s in (0, 0, 0.5, 0.5, 1, 1, 1)],
                         daemon=True).start()
