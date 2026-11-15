"""Import der ACC-MoTeC-Exporte (.ld + .ldx) in das interne DataFrame-Schema.

Der ACC-Export enthält nur einen Teil der Kanäle (siehe CHANNEL_MAP). Fehlende Kanäle werden
als NaN angelegt; die Analyse lässt davon abhängige Auswertungen dann weg.

Eine .ld-Datei kann mehrere Sessions enthalten (z. B. Quali + Rennen, mehrmals hintereinander).
Sessions werden an langen Stillständen (> SESSION_GAP s) getrennt; innerhalb einer Session wird jede
Runde klassifiziert (Start, Outlap, gezeitet, Box/Stopp, Inlap).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from .schema import WHEELS
from .vendor import ldparser

HZ = 60
G = 9.81
WMAP = {"lf": "fl", "rf": "fr", "lr": "rl", "rr": "rr"}

STOP_SPEED = 0.5      # m/s – darunter gilt das Auto als stehend
STOP_MIN = 5.0        # s – kürzere Stillstände ignorieren
SESSION_GAP = 120.0   # s – Stillstand ab dieser Länge trennt Sessions (Boxenstopps/Return-to-Pit sind kürzer)
NOISE_DIST = 100.0    # m – Fahrstücke darunter zwischen zwei Stillständen sind Rauschen (Rollen in der Box)

KIND_START = "Start"          # Rennstart: vom Grid bis zur ersten Linienüberquerung
KIND_OUT = "Outlap"           # aus der Box bis zur ersten Linienüberquerung
KIND_TIMED = "gezeitet"       # Linie bis Linie ohne Stillstand
KIND_STOP = "Box/Stopp"       # Linie bis Linie mit Stillstand (Boxenstopp, Return-to-Pit)
KIND_IN = "Inlap"             # letzte Linienüberquerung bis Session-Ende


def read_ld(path: Path) -> dict[str, tuple[float, str, np.ndarray]]:
    d = ldparser.ldData.fromfile(str(path))
    ch = {}
    for c in d.channs:
        x = np.asarray(c.data, dtype=float)
        if len(x):
            ch[c.name] = (float(c.freq), c.unit, x)
    return ch, d.head


def _key(stem: str) -> str:
    """Vergleichsschlüssel: Upload-Präfix (8 Hex) weg, Sonderzeichen vereinheitlicht ('.' und '_' im Datum)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"^[0-9a-f]{8}-", "", stem))


def read_beacons(ld_path: Path) -> tuple[list[float], dict]:
    """Rundengrenzen (s) und Details aus der .ldx neben der .ld."""
    p = ld_path.with_suffix(".ldx")
    if not p.exists():
        cand = [q for q in ld_path.parent.glob("*.ldx") if _key(q.stem) == _key(ld_path.stem)]
        if not cand:
            return [], {}
        p = cand[0]
    root = ET.parse(p).getroot()
    beacons = sorted(float(m.get("Time")) / 1e6 for m in root.iter("Marker") if m.get("ClassName") == "BCN")
    details = {s.get("Id"): s.get("Value") for s in root.iter("String")}
    return beacons, details


def time_resets(ch) -> list[float]:
    """Linienüberquerungen aus dem TIME-Kanal (laufende Rundenzeit, springt an der Linie auf 0).

    Ergänzt die .ldx-Beacons: dort fehlt die letzte Überquerung, wenn die Aufnahme genau an der Ziellinie endet.
    """
    if "TIME" not in ch:
        return []
    f, _, x = ch["TIME"]
    if "SPEED" in ch and len(x) / f > 1.5 * len(ch["SPEED"][2]) / ch["SPEED"][0] + 30:
        return []   # TIME viel länger als die Kerndaten: Einzelrunden-Export, TIME stammt aus der ganzen Session
    d = np.diff(x)
    idx = np.where((d < -1.0) & (x[:-1] > 10.0))[0]
    return [float((i + 1) / f) for i in idx]


def parse_name(path: Path) -> dict:
    stem = re.sub(r"^[0-9a-f]{8}-", "", path.stem)  # evtl. Upload-Präfix entfernen
    m = re.match(r"^(?P<track>[^-]+)-(?P<car>.+)-(?P<n>\d+)-(?P<date>\d{4}[._]\d\d[._]\d\d-\d\d[._]\d\d[._]\d\d)$", stem)
    return m.groupdict() if m else {"track": "?", "car": stem}


def _grid(ch, name, t, mode="interp"):
    f, _, x = ch[name]
    tt = np.arange(len(x)) / f
    if mode == "max" and f > HZ:
        w = int(np.ceil(f / HZ))
        x = pd.Series(x).rolling(w, min_periods=1).max().values
    return np.interp(t, tt, x)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Zusammenhängende True-Bereiche als (start, end_exklusiv)."""
    m = np.concatenate([[False], mask, [False]]).astype(int)
    d = np.diff(m)
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def find_stops(t: np.ndarray, v: np.ndarray) -> list[tuple[float, float]]:
    """Stillstände (Start, Ende) in s; kurzes Rollen zwischen zwei Stillständen wird zusammengefasst."""
    moving = v > STOP_SPEED
    for a, b in _runs(moving):   # Rauschen: kaum Strecke gefahren -> zählt als Stillstand
        if np.sum(v[a:b]) / HZ < NOISE_DIST:
            moving[a:b] = False
    return [(float(t[a]), float(t[b - 1])) for a, b in _runs(~moving) if (b - a) / HZ >= STOP_MIN]


def detect_sessions(T: float, stops: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Fahrphasen (Start, Ende) zwischen langen Stillständen."""
    breaks = [(a, b) for a, b in stops if b - a >= SESSION_GAP or a <= 0.5 or b >= T - 0.5]
    sessions, cur = [], 0.0
    for a, b in breaks:
        if a - cur > STOP_MIN:
            sessions.append((cur, a))
        cur = max(cur, b)
    if T - cur > STOP_MIN:
        sessions.append((cur, T))
    return sessions


def build_laps(t, v, dist, sessions, beacons, stops, time_cross=()):
    """Runden je Session mit Typ, Dauer und gefahrener Strecke."""
    beacons = sorted(set(beacons) | {x for x in time_cross if all(abs(x - b) > 0.5 for b in beacons)})
    raw = []
    for si, (s0, s1) in enumerate(sessions):
        inner = [b for b in beacons if s0 + 0.5 < b < s1 - 0.5]
        edges = [s0] + inner + [s1]
        for k in range(len(edges) - 1):
            a, b = edges[k], edges[k + 1]
            i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b)
            d = float(dist[min(i1, len(dist) - 1)] - dist[min(i0, len(dist) - 1)])
            stopped = any(sa < b and sb > a for sa, sb in stops)
            ends_at_line = k < len(edges) - 2 or any(abs(b - x) <= 1.0 for x in beacons)
            starts_at_line = k > 0
            raw.append(dict(session=si, k=k, start=a, end=b, dur=b - a, dist=d, stopped=stopped,
                            starts_at_line=starts_at_line, ends_at_line=ends_at_line))
    # Referenz-Rundenlänge: Runden von Linie zu Linie ohne Stillstand
    full = [r["dist"] for r in raw if r["starts_at_line"] and r["ends_at_line"] and not r["stopped"]]
    ref = float(np.median(full)) if full else float(np.median([r["dist"] for r in raw])) if raw else 0.0

    laps = []
    for r in raw:
        if not r["starts_at_line"]:
            kind = KIND_START if r["dist"] < 0.6 * ref else KIND_OUT
        elif not r["ends_at_line"]:
            kind = KIND_IN
        elif r["stopped"]:
            kind = KIND_STOP
        else:
            kind = KIND_TIMED
        if kind == KIND_IN and r["dist"] < 0.05 * ref:
            continue   # nur ein paar Meter nach der Ziellinie bis zum Stillstand
        laps.append(dict(r, kind=kind))
    full_ok = [l for l in laps if l["kind"] == KIND_TIMED and ref > 0 and 0.97 * ref <= l["dist"] <= 1.04 * ref]
    best = min((l["dur"] for l in full_ok), default=None)
    for i, l in enumerate(laps):
        l["lap"] = i
        l["valid"] = bool(l in full_ok and best and l["dur"] <= 1.2 * best)
    # Rundennummer innerhalb der Session: gezeitete Runden ab 1, Start/Outlap = 0
    for si in range(len(sessions)):
        n = 0
        for l in (l for l in laps if l["session"] == si):
            if l["k"] > 0:
                n += 1
            l["num"] = n
    return laps, ref


def _is_race(ls: list[dict], ref_speed: float) -> bool:
    """Rennen: Session beginnt mit Grid-Start (kurzes Stück bis zur Linie) oder mit einer langsamen
    Formationsrunde (Ø-Tempo deutlich unter dem einer normalen Runde)."""
    if not ls:
        return False
    first = ls[0]
    if first["kind"] == KIND_START:
        return True
    if first["kind"] == KIND_OUT and ref_speed > 0 and first["dur"] > 0:
        return first["dist"] / first["dur"] < 0.55 * ref_speed and not first["stopped"]
    return False


def session_info(sessions, laps, stops) -> list[dict]:
    out = []
    timed_all = [l for l in laps if l["valid"]]
    ref_speed = float(np.median([l["dist"] / l["dur"] for l in timed_all])) if timed_all else 0.0
    for si, (s0, s1) in enumerate(sessions):
        ls = [l for l in laps if l["session"] == si]
        timed = [l for l in ls if l["valid"]]
        n_stops = sum(1 for a, b in stops if s0 < a and b < s1)
        out.append(dict(
            id=si, start=s0, end=s1, dur=s1 - s0,
            type="Rennen" if _is_race(ls, ref_speed) else "Training/Quali",
            laps=len(ls), laps_valid=len(timed), stops=n_stops,
            best=min((l["dur"] for l in timed), default=float("nan")),
            median=float(np.median([l["dur"] for l in timed])) if timed else float("nan"),
        ))
    return out


def _single_lap_export(v, dist, stops, beacons, lap_info, ref) -> bool:
    """Aufnahme ist eine einzelne fliegende Runde: keine gültige Runde gefunden, keine Beacons im Bereich, kein Stillstand,
    Start und Ende in voller Fahrt (> 54 km/h), Länge 2–8 km."""
    if any(l["valid"] for l in lap_info) or stops or len(v) < 2:
        return False
    if any(0 < b < len(v) / HZ - 1 for b in beacons):
        return False
    d = float(dist[-1])
    return v[0] > 15 and v[-1] > 15 and 2000 < d < 8000


def load_ld(path: str | Path) -> tuple[pd.DataFrame, dict]:
    path = Path(path)
    ch, head = read_ld(path)
    # Die Kerndaten (SPEED) bestimmen die Länge; Zusatzkanäle (EN_*, TIME) laufen teils länger.
    T = (len(ch["SPEED"][2]) / ch["SPEED"][0]) if "SPEED" in ch else min(len(x) / f for f, _, x in ch.values())
    t = np.arange(0, T, 1.0 / HZ)
    n = len(t)
    nan = np.full(n, np.nan)
    df = pd.DataFrame({"t": t})

    def has(k): return k in ch
    g = lambda k, **kw: _grid(ch, k, t, **kw) if has(k) else nan

    df["speed"] = g("SPEED") * 3.6
    df["gas"] = g("THROTTLE") / 100
    df["brake"] = g("BRAKE") / 100
    df["steer"] = g("STEERANGLE")
    df["gear"] = np.rint(g("GEAR")) - 1 if has("GEAR") else nan  # ACC-Export: 0=R? 1=N -> hier grob
    df["rpm"] = g("RPMS")
    # Achtung: Der Export beschriftet G_LAT/G_LON als m/s², die Werte liegen aber bei ca. ±2 -> tatsächlich in g.
    # Robust: nur teilen, wenn das 99. Perzentil nach m/s² aussieht (>5).
    def _acc(name):
        x = g(name)
        return x / G if np.nanpercentile(np.abs(x), 99) > 5 else x
    df["g_lat"] = _acc("G_LAT")
    df["g_lon"] = _acc("G_LON")
    df["yaw"] = g("ROTY")
    df["tcs"] = g("TC")
    df["abs"] = g("ABS")
    for lw, w in WMAP.items():
        L = lw.upper()
        df[f"w_{w}"] = g(f"WHEEL_SPEED_{L}")        # m/s (linear) – Radius kürzt sich in der Schlupfberechnung
        df[f"p_{w}"] = g(f"TYRE_PRESS_{L}")         # psi
        df[f"tc_{w}"] = g(f"TYRE_TAIR_{L}")         # °C (Reifentemperatur, kein I/M/O)
        df[f"bt_{w}"] = g(f"BRAKE_TEMP_{L}")        # °C
        df[f"st_{w}"] = g(f"SUS_TRAVEL_{L}")        # mm
        df[f"bsu_{w}"] = g(f"BUMPSTOPUP_RIDE_{L}", mode="max")
        df[f"bsd_{w}"] = g(f"BUMPSTOPDN_RIDE_{L}", mode="max")
        df[f"bsf_{w}"] = g(f"BUMPSTOP_FORCE_{L}")
        for pre in ("ws", "fz", "ti", "tm", "to"):
            df[f"{pre}_{w}"] = nan                  # nicht im Export enthalten
    for c in ("fuel", "bb", "air", "road", "tyres_out", "rh_f", "rh_r", "pit"):
        df[c] = nan
    df["pit"] = 0

    # Sessions (lange Stillstände) und Runden (Beacons aus der .ldx + Sprünge im TIME-Kanal)
    beacons, details = read_beacons(path)
    v = df["speed"].fillna(0).values / 3.6
    dist = np.cumsum(v) / HZ
    stops = find_stops(t, v)
    sessions = detect_sessions(T, stops)
    lap_info, ref = build_laps(t, v, dist, sessions, [b for b in beacons if 0 < b < T], stops,
                               [x for x in time_resets(ch) if 0 < x <= T + 0.5])
    if _single_lap_export(v, dist, stops, beacons, lap_info, ref):
        # Export genau einer fliegenden Runde (z. B. Referenz-Hotlap ohne .ldx): ganze Aufnahme = eine gültige Runde
        lap_info = [dict(session=0, k=1, start=0.0, end=T, dur=T, dist=float(dist[-1]), stopped=False,
                         starts_at_line=True, ends_at_line=True, kind=KIND_TIMED, lap=0, valid=True, num=1)]
        sessions, ref = [(0.0, T)], float(dist[-1])

    lap = np.full(n, -1, dtype=int)          # -1 = zwischen Sessions (Garage/Wartezeit)
    sess = np.full(n, -1, dtype=int)
    pos = np.zeros(n)
    lap_ms = np.zeros(n)
    for l in lap_info:
        m = (t >= l["start"]) & (t < l["end"])
        if not m.any():
            continue
        lap[m] = l["lap"]
        sess[m] = l["session"]
        d = dist[m] - dist[m][0]
        pos[m] = d / (d[-1] if d[-1] > 0 else 1)
        lap_ms[m] = (t[m] - l["start"]) * 1000.0
    df["lap"] = lap
    df["session"] = sess
    df["pos"] = pos
    df["lap_ms"] = lap_ms
    moving = bool(dist[-1] > 500)

    meta = parse_name(path)
    meta.update(source=path.name, venue=head.venue, laps_info=lap_info, details=details, has_beacons=bool(beacons),
                sessions=session_info(sessions, lap_info, stops), compound="dry_compound", moving=moving,
                ref_lap_m=ref, source_type="ld",
                rec_start=head.datetime.timestamp() if getattr(head, "datetime", None) else None)   # Ortszeit Aufnahmebeginn
    return df, meta
