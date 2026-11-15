"""Live-Auswertung ohne MoTeC-File: puffert die laufende ACC-Session aus dem Shared Memory (ca. 25 Hz) und baut
daraus dasselbe DataFrame/meta wie der MoTeC-Import (ldimport.load_ld). Damit laufen Analyse, Empfehlungen und
Setup-Export schon während der Session – z. B. mit dem Aggressive-Preset auf einer neuen Strecke.

Zusätzlich zum MoTeC-Export liefert ACC hier Schräglaufwinkel (slipAngle) und Radschlupf (wheelSlip) je Rad –
die direkteste Messung für Unter-/Übersteuern.
"""
from __future__ import annotations

import math
import statistics
import threading

import numpy as np
import pandas as pd

from .ldimport import KIND_OUT, KIND_START, KIND_STOP, KIND_TIMED
from .schema import WHEELS

TYPE_DE = {0: "Training", 1: "Quali", 2: "Rennen"}
_W4 = ("w", "p", "tc", "bt", "st", "ws", "sa")
RAW = (["t", "laps", "cur_ms", "last_ms", "npos", "pit", "speed", "gas", "brake", "steer", "gear", "rpm",
        "g_lat", "g_lon", "yaw", "tc_act", "abs_act"]
       + [f"{pre}_{w}" for pre in _W4 for w in WHEELS]
       + ["fuel", "bb", "air", "road", "left"])
COL = {c: i for i, c in enumerate(RAW)}
CHUNK = 20000          # Zeilen je Speicherblock (≈ 13 min bei 25 Hz, ≈ 4 MB)
MIN_GAP = 2.0          # s – Lücke in der Aufnahme (Pause, Replay) -> Runde zählt nicht


class Recorder:
    """Puffer der aktuellen Session. add() kommt aus dem Mitschnitt-Thread, snapshot() aus der Auswertung."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self, key=None, info=None):
        with getattr(self, "_lock", threading.Lock()):
            self._chunks: list[np.ndarray] = []
            self._n = 0
            self.key = key
            self.info = dict(info or {})
            self.first_laps = None
            self.laps_done = 0
            self._last_laps = None
            self._last_left = None

    @property
    def samples(self) -> int:
        return self._n

    def add(self, p, g, s, now: float, car: str, track: str):
        """Eine Zeile aus physics/graphics/static anhängen (nur aufrufen, wenn ACC live ist)."""
        laps, left = int(g.completedLaps), float(g.sessionTimeLeft) / 1000.0
        key = (int(g.session), car, track)
        restart = (self._last_laps is not None and laps < self._last_laps) or \
                  (self._last_left is not None and left > self._last_left + 120)
        if key != self.key or restart:
            from .shm import wstr
            self.reset(key, dict(session=int(g.session), car=car, track=track, compound=wstr(g.tyreCompound) or "dry_compound"))
        self._last_laps, self._last_left = laps, left
        if g.isInPit and p.speedKmh < 1:      # steht in der Box/Garage -> Puffer nicht unnötig füllen
            return
        row = [now, laps, g.iCurrentTime, g.iLastTime, g.normalizedCarPosition, int(bool(g.isInPit or g.isInPitLane)),
               p.speedKmh, p.gas, p.brake, p.steerAngle, p.gear, p.rpms,
               p.accG[0], p.accG[2], p.localAngularVel[1], p.tcinAction, p.absInAction]
        for arr, scale in ((p.wheelAngularSpeed, 1.0), (p.wheelsPressure, 1.0), (p.tyreCoreTemperature, 1.0),
                           (p.brakeTemp, 1.0), (p.suspensionTravel, 1000.0), (p.wheelSlip, 1.0), (p.slipAngle, 1.0)):
            row += [arr[i] * scale for i in range(4)]
        row += [p.fuel, p.brakeBias, p.airTemp, p.roadTemp, left]
        with self._lock:
            i = self._n % CHUNK
            if i == 0:
                self._chunks.append(np.full((CHUNK, len(RAW)), np.nan, dtype=np.float64))
            self._chunks[-1][i] = row
            self._n += 1
            if self.first_laps is None:
                self.first_laps = laps
            self.laps_done = laps - self.first_laps

    def snapshot(self) -> tuple[np.ndarray, dict]:
        with self._lock:
            if not self._n:
                return np.empty((0, len(RAW))), dict(self.info)
            data = np.concatenate(self._chunks)[: self._n].copy()
            return data, dict(self.info)


def build(raw: np.ndarray, info: dict) -> tuple[pd.DataFrame, dict]:
    """Rohzeilen (RAW) -> (df, meta) im Format von ldimport.load_ld."""
    n = len(raw)
    R = {c: raw[:, i] for c, i in COL.items()}
    t_abs = R["t"]
    t = t_abs - t_abs[0]
    nan = np.full(n, np.nan)
    df = pd.DataFrame({"t": t})
    df["speed"] = R["speed"]
    df["gas"], df["brake"] = R["gas"], R["brake"]
    df["steer"], df["gear"], df["rpm"] = R["steer"], R["gear"] - 1, R["rpm"]
    df["g_lat"], df["g_lon"], df["yaw"] = R["g_lat"], R["g_lon"], R["yaw"]
    df["tcs"], df["abs"] = R["tc_act"], R["abs_act"]
    sa = np.column_stack([R[f"sa_{w}"] for w in WHEELS])
    if np.nanpercentile(np.abs(sa), 95) < 0.35:   # ACC liefert Radiant -> in Grad
        sa = np.degrees(sa)
    for i, w in enumerate(WHEELS):
        df[f"w_{w}"] = np.abs(R[f"w_{w}"])            # rad/s – Radius kürzt sich in der Schlupfberechnung
        df[f"p_{w}"] = R[f"p_{w}"]
        df[f"tc_{w}"] = R[f"tc_{w}"]
        df[f"bt_{w}"] = R[f"bt_{w}"]
        df[f"st_{w}"] = R[f"st_{w}"]
        df[f"ws_{w}"] = R[f"ws_{w}"]
        df[f"sa_{w}"] = sa[:, i]
        for pre in ("bsu", "bsd", "bsf", "fz", "ti", "tm", "to"):
            df[f"{pre}_{w}"] = nan   # I/M/O-Temperaturen liefert ACC nicht zuverlässig -> keine Sturz-Empfehlung
    df["fuel"], df["bb"], df["air"], df["road"] = R["fuel"], R["bb"], R["air"], R["road"]
    for c in ("tyres_out", "rh_f", "rh_r"):
        df[c] = nan
    df["pit"] = R["pit"]

    dt = np.clip(np.diff(t, prepend=t[0]), 0, 0.5)
    dist = np.cumsum(R["speed"] / 3.6 * dt)
    gap = np.diff(t, prepend=t[0]) > MIN_GAP
    lapc = R["laps"].astype(int)
    edges = [0] + list(np.where(np.diff(lapc) != 0)[0] + 1) + [n]
    segs = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1) if edges[k + 1] > edges[k]]
    typ = int(info.get("session", 0))

    raw_laps = []
    for k, (i0, i1) in enumerate(segs[:-1]):     # letzte Runde läuft noch -> nicht auswerten
        j = min(np.searchsorted(t, t[i1] + 1.0), segs[k + 1][1] - 1)   # ACC setzt iLastTime kurz nach der Linie
        lap_ms = R["last_ms"][j]
        dur = lap_ms / 1000.0 if lap_ms and lap_ms > 0 else t[i1] - t[i0]
        pit = bool(np.nanmax(R["pit"][i0:i1]) > 0)
        if k == 0:
            kind = KIND_START if typ == 2 and not R["pit"][i0] else KIND_OUT
        else:
            kind = KIND_STOP if pit else KIND_TIMED
        raw_laps.append(dict(session=0, k=k, start=float(t[i0]), end=float(t[i1]), i0=i0, i1=i1, dur=float(dur),
                             dist=float(dist[i1 - 1] - dist[i0]), stopped=pit, kind=kind,
                             gap=bool(gap[i0 + 1:i1].any())))
    full = [l["dist"] for l in raw_laps if l["kind"] == KIND_TIMED]
    ref = float(np.median(full)) if full else 0.0
    ok = [l for l in raw_laps if l["kind"] == KIND_TIMED and not l["gap"] and ref and 0.97 * ref <= l["dist"] <= 1.04 * ref]
    best = min((l["dur"] for l in ok), default=None)

    lap = np.full(n, -1, dtype=int)
    pos = np.zeros(n)
    laps_info = []
    for i, l in enumerate(raw_laps):
        i0, i1 = l.pop("i0"), l.pop("i1")
        l.pop("gap")
        l.update(lap=i, num=l["k"], valid=bool(l in ok and best and l["dur"] <= 1.2 * best))
        lap[i0:i1] = i
        pp = R["npos"][i0:i1].copy()
        tt = t[i0:i1] - t[i0]
        pp[(tt < 3) & (pp > 0.9)] = 0.0            # Linie überquert, Position noch nicht zurückgesetzt
        pp[(tt > tt[-1] - 3) & (pp < 0.1)] = 1.0
        if l["kind"] != KIND_TIMED:                # Out-/Startrunde: nicht ab der Linie -> über die Strecke normieren
            d = dist[i0:i1] - dist[i0]
            pp = d / (d[-1] if d[-1] > 0 else 1)
        pos[i0:i1] = pp
        laps_info.append(l)
    df["lap"] = lap
    df["session"] = np.where(lap >= 0, 0, -1)
    df["pos"] = pos
    df["lap_ms"] = R["cur_ms"]

    timed = [l for l in laps_info if l["valid"]]
    lefts = R["left"][R["left"] > 0]
    live = dict(type=typ, type_name=TYPE_DE.get(typ, "?"), n=n,
                air=float(np.nanmedian(R["air"])), road=float(np.nanmedian(R["road"])),
                minutes=int(math.ceil(float(lefts.max()) / 60.0 - 0.05)) if len(lefts) else None,
                fuel_start=float(R["fuel"][0]), fuel_end=float(R["fuel"][-1]))
    sess = dict(id=0, start=float(t[0]), end=float(t[-1]), dur=float(t[-1] - t[0]),
                type=TYPE_DE.get(typ, "Training"), laps=len(laps_info), laps_valid=len(timed), stops=0,
                best=min((l["dur"] for l in timed), default=float("nan")),
                median=float(statistics.median(l["dur"] for l in timed)) if timed else float("nan"), live=live)
    meta = dict(track=info.get("track", "?"), car=info.get("car", "?"), source="Live (ACC)", venue=info.get("track"),
                laps_info=laps_info, details={}, has_beacons=True, sessions=[sess], compound=info.get("compound", "dry_compound"),
                moving=bool(dist[-1] > 500) if n else False, ref_lap_m=ref, source_type="live",
                rec_start=float(t_abs[0]) if n else None, live_sessions=1)
    return df, meta


def default_mode(meta: dict) -> str:
    """Quali-Session -> Quali-Auswertung, sonst Rennen (Stint-Logik, 1. Runde je Stint nicht im Druck-Ø)."""
    typ = ((meta.get("sessions") or [{}])[0].get("live") or {}).get("type")
    return "quali" if typ in (1, 8) else "race"
