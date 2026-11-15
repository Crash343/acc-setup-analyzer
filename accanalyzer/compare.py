"""Vergleich deiner Runde mit einer Referenzrunde (z. B. Hotlap eines Esport-Fahrers als MoTeC-File).

Beide Runden werden über die gefahrene Strecke (0..1 der Runde, in Metern der Referenz) übereinandergelegt.
Je Abschnitt (Kurve inkl. Bremszone bis zur nächsten Kurve) wird ermittelt: Zeitverlust, Bremspunkt, Bremsdruck,
Kurvenminimum, Vollgas-Punkt und Lenkaufwand. Daraus entstehen die Hinweise "wo verliere ich Zeit und warum".

Referenz-Dateien liegen in  Dokumente\\ACC-Analyse\\Referenz  (beliebige Unterordner, .ld + .ldx, optional Setup .json).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis

N = 2000                 # Stützstellen je Runde
NB = analysis.CORNER_BINS
BRAKE_ON = 0.2           # Bremse > 20 % = Bremspunkt
GAS_FULL = 0.9           # Gas > 90 % = Vollgas


def ref_dir() -> Path:
    from .livelog import docs_dir
    return docs_dir() / "ACC-Analyse" / "Referenz"


def find_refs(meta: dict | None = None) -> list[Path]:
    """Alle Referenz-.ld; passende Strecke/Auto zuerst."""
    d = ref_dir()
    try:
        files = sorted(d.rglob("*.ld"))
    except OSError:
        return []
    track = str((meta or {}).get("track", "")).lower()
    car = str((meta or {}).get("car", "")).lower()

    def score(p: Path):
        n = str(p).lower()
        return (-(bool(track) and track in n) * 2 - (bool(car) and car in n), n)
    return sorted(files, key=score)


def ref_setup(ld: Path) -> Path | None:
    """Setup zur Referenz: gleichnamige .json oder die einzige .json im selben Ordner."""
    p = Path(ld).with_suffix(".json")
    if p.exists():
        return p
    js = list(Path(ld).parent.glob("*.json"))
    return js[0] if len(js) == 1 else None


def valid_laps(meta: dict) -> list[dict]:
    return sorted((l for l in meta.get("laps_info") or [] if l["valid"]), key=lambda l: l["dur"])


def best_lap(meta: dict) -> dict | None:
    v = valid_laps(meta)
    return v[0] if v else None


def lap_trace(df: pd.DataFrame, lap: dict) -> pd.DataFrame:
    """Eine Runde auf N gleich verteilte Streckenpunkte (x = 0..1) interpoliert."""
    g = df[df.lap == lap["lap"]]
    x = np.maximum.accumulate(g.pos.values)
    x, idx = np.unique(x, return_index=True)          # Stillstand/Rundung -> doppelte Positionen weg
    grid = np.linspace(0, 1, N)
    out = {"x": grid}
    for c in ("t", "speed", "gas", "brake", "steer", "g_lat", "gear"):
        out[c] = np.interp(grid, x, g[c].values[idx]) if c in g else np.full(N, np.nan)
    out["t"] = out["t"] - out["t"][0]
    return pd.DataFrame(out)


def track_xy(df: pd.DataFrame, lap: dict) -> tuple[np.ndarray, np.ndarray] | None:
    """Streckenform aus einer Runde (Koppelnavigation, das MoTeC-File hat keine Weltkoordinaten).

    Kurs = Integral der Gierrate (Ersatz: Querbeschleunigung / Tempo), Weg = Integral des Tempos. Die Drift wird über
    die Runde verteilt, sodass der Kurs genau eine volle Drehung macht und die Runde sich schließt.
    Rückgabe: x, y in Metern auf den N Streckenpunkten von lap_trace (Start/Ziel bei (0, 0), Fahrtrichtung +x).
    """
    g = df[df.lap == lap["lap"]]
    if len(g) < 50:
        return None
    t = g.t.values
    v = np.nan_to_num(g.speed.values / 3.6)
    w = g.yaw.values if "yaw" in g and np.isfinite(g.yaw.values).mean() > 0.9 else None
    if w is None:
        if "g_lat" not in g or not np.isfinite(g.g_lat.values).any():
            return None
        w = g.g_lat.values * 9.81 / np.maximum(v, 5.0)
    dt = np.diff(t, prepend=t[0])
    h = np.cumsum(np.nan_to_num(w) * dt)
    s = np.cumsum(v * dt)
    if s[-1] <= 0:
        return None
    f = s / s[-1]
    turns = np.round(h[-1] / (2 * np.pi))
    if turns:
        h = h - f * (h[-1] - turns * 2 * np.pi)       # genau eine volle Drehung
    x, y = np.cumsum(v * np.cos(h) * dt), np.cumsum(v * np.sin(h) * dt)
    x, y = x - f * x[-1], y - f * y[-1]                # Runde schließen
    pos, idx = np.unique(np.maximum.accumulate(g.pos.values), return_index=True)
    grid = np.linspace(0, 1, N)
    return np.interp(grid, pos, x[idx]), np.interp(grid, pos, y[idx])


def _first(mask: np.ndarray) -> int | None:
    return int(np.argmax(mask)) if len(mask) and mask.any() else None


def compare(own_df, own_meta, ref_df, ref_meta, own_lap: dict | None = None, ref_lap: dict | None = None) -> dict:
    own_lap = own_lap or best_lap(own_meta)
    ref_lap = ref_lap or best_lap(ref_meta)
    if own_lap is None or ref_lap is None:
        raise ValueError("Keine gültige Runde " + ("in deiner Aufnahme" if own_lap is None else "in der Referenz"))
    o, r = lap_trace(own_df, own_lap), lap_trace(ref_df, ref_lap)
    L = float(ref_lap.get("dist") or ref_meta.get("ref_lap_m") or 0) or float(own_lap.get("dist") or 1)
    # Zeit je Streckenpunkt auf die offizielle Rundenzeit skalieren (Interpolation/Abtastung)
    o["t"] *= own_lap["dur"] / o.t.iloc[-1] if o.t.iloc[-1] > 0 else 1
    r["t"] *= ref_lap["dur"] / r.t.iloc[-1] if r.t.iloc[-1] > 0 else 1
    tr = pd.DataFrame({"x": o.x, "m": o.x * L, "delta": o.t - r.t})
    for c in ("speed", "gas", "brake", "steer", "gear"):
        tr[f"{c}_o"], tr[f"{c}_r"] = o[c].values, r[c].values

    # Abschnitte aus dem Profil der Referenz (Kurve + Gerade bis zur nächsten Bremszone)
    b = np.minimum((r.x.values * NB).astype(int), NB - 1)
    gprof = pd.Series(r.g_lat.values).groupby(b).median().reindex(range(NB)).fillna(0).values
    bprof = pd.Series(r.brake.values).groupby(b).median().reindex(range(NB)).fillna(0).values
    runs = analysis.corner_runs(gprof, bprof)
    sections = []
    steer_scale = max(np.nanpercentile(np.abs(tr.steer_r), 99), 1e-6)
    for k, (a0, a, e) in enumerate(runs):
        end = runs[k + 1][0] if k + 1 < len(runs) else NB
        start = a0 if k else 0          # Start/Ziel-Gerade gehört zur 1. Kurve
        i0, ia, ie, i1 = (int(v / NB * (N - 1)) for v in (start, a, e, end))
        prev_e = runs[k - 1][2] if k else 0            # Bremspunkt: nach der vorigen Kurve, etwas vor der Bremszone
        ib = int(max(prev_e, a0 - 15) / NB * (N - 1))
        sec = dict(no=k + 1, x0=start / NB, x1=end / NB, apex=float(tr.x[ia + int(np.argmax(np.abs(r.g_lat.values[ia:ie + 1])))]),
                   dt=float(tr.delta[i1 if i1 < N else N - 1] - tr.delta[i0]))
        for side in ("o", "r"):
            bz = tr[f"brake_{side}"].values[ib:max(ib + 1, int(sec["apex"] * (N - 1)) + 1)]
            j = _first(bz > BRAKE_ON)
            sec[f"brake_{side}"] = float(tr.m[ib + j]) if j is not None else None
            sec[f"bmax_{side}"] = float(np.nanmax(bz)) if len(bz) else None
            sec[f"vmin_{side}"] = float(np.nanmin(tr[f"speed_{side}"].values[ia:ie + 1]))
            apex_i = ia + int(np.argmin(tr[f"speed_{side}"].values[ia:ie + 1]))
            j = _first(tr[f"gas_{side}"].values[apex_i:i1] > GAS_FULL)
            sec[f"gas_{side}"] = float(tr.m[apex_i + j]) if j is not None else None
            sec[f"steer_{side}"] = float(np.nanmean(np.abs(tr[f"steer_{side}"].values[ia:ie + 1])) / steer_scale)
        sec["why"] = _why(sec)
        sections.append(sec)
    total = float(own_lap["dur"] - ref_lap["dur"])
    xy = track_xy(ref_df, ref_lap) or track_xy(own_df, own_lap)
    return dict(trace=tr, sections=sections, own_lap=own_lap, ref_lap=ref_lap, total=total, length=L,
                summary=_summary(sections, total), map=xy, worst=worst_sections(sections))


def worst_sections(sections: list[dict], n: int = 3) -> list[dict]:
    """Die n Abschnitte mit dem größten Zeitverlust (nur echte Verluste > 0,05 s)."""
    return sorted((s for s in sections if s["dt"] > 0.05), key=lambda s: -s["dt"])[:n]


def _why(s: dict) -> list[str]:
    """Kurze Begründungen für einen Abschnitt (nur deutliche Unterschiede)."""
    out = []
    if s["brake_o"] is not None and s["brake_r"] is not None:
        d = s["brake_r"] - s["brake_o"]
        if abs(d) >= 8:
            out.append(f"bremst {abs(d):.0f} m {'früher' if d > 0 else 'später'}")
    elif s["brake_o"] is not None and s["brake_r"] is None:
        out.append("bremst, Referenz nicht")
    elif s["brake_o"] is None and s["brake_r"] is not None:
        out.append("bremst nicht, Referenz schon")
    if s["bmax_o"] is not None and s["bmax_r"] is not None and s["bmax_r"] > 0.3 and s["bmax_o"] < s["bmax_r"] - 0.12:
        out.append(f"weniger Bremsdruck ({s['bmax_o'] * 100:.0f} % statt {s['bmax_r'] * 100:.0f} %)")
    dv = s["vmin_o"] - s["vmin_r"]
    if abs(dv) >= 3:
        out.append(f"Kurvenminimum {abs(dv):.0f} km/h {'langsamer' if dv < 0 else 'schneller'}")
    if s["gas_o"] is not None and s["gas_r"] is not None:
        d = s["gas_o"] - s["gas_r"]
        if abs(d) >= 10:
            out.append(f"Vollgas {abs(d):.0f} m {'später' if d > 0 else 'früher'}")
    elif s["gas_o"] is None and s["gas_r"] is not None:
        out.append("kein Vollgas bis zur nächsten Kurve")
    if s["steer_r"] > 0.05 and s["steer_o"] > 1.2 * s["steer_r"]:
        out.append(f"{(s['steer_o'] / s['steer_r'] - 1) * 100:.0f} % mehr Lenkeinschlag (Auto schiebt/rutscht?)")
    return out


def _summary(sections: list[dict], total: float) -> list[str]:
    lost = worst_sections(sections)
    out = [f"Rundenzeit {'+' if total >= 0 else '−'}{abs(total):.3f} s gegenüber der Referenz."]
    for s in lost:
        out.append(f"K{s['no']} ({s['apex'] * 100:.0f} %): {s['dt']:+.2f} s – " + (", ".join(s["why"]) or "kein einzelner deutlicher Unterschied"))
    won = [s for s in sections if s["dt"] < -0.05]
    if won:
        out.append("Schneller als die Referenz: " + ", ".join(f"K{s['no']} ({s['dt']:+.2f} s)" for s in sorted(won, key=lambda s: s["dt"])[:3]))
    return out


# ----------------------------------------------------------------------------- Setup-Vergleich
def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list) and all(isinstance(x, (int, float)) for x in obj):
        for i, v in enumerate(obj):
            out[f"{prefix}[{i}]"] = v
    else:
        out[prefix] = obj
    return out


def setup_diff(own: dict, ref: dict) -> list[tuple[str, object, object]]:
    """Alle Werte, die sich unterscheiden: [(Pfad, deins, Referenz)] (ohne Name/Spritmenge-Metadaten)."""
    a, b = _flatten(own), _flatten(ref)
    skip = ("carName", "basicSetup.strategy.fuel", "trackBopType")
    keys = [k for k in dict.fromkeys(list(a) + list(b)) if not k.startswith(skip)]
    return [(k, a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)]


def load_setup(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))
