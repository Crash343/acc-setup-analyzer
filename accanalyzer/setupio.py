"""ACC-Setup-JSON lesen und Änderungsvorschläge in eine NEUE Datei schreiben.

Setups liegen unter  Documents/Assetto Corsa Competizione/Setups/<Auto>/<Strecke>/*.json
Alle Werte darin sind Clicks (ganzzahlige Indizes). Radreihenfolge: [VL, VR, HL, HR].
Pfade und Click-Logik orientieren sich an Setup Ripple (MIT), siehe knowledge.py.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path

from . import knowledge as K

MAX_PACKAGES = 2  # pro Fahrt nur 1-2 Änderungen testen


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))  # ACC schreibt teils mit BOM


def _get(obj, path):
    for k in path.split("."):
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


def _set_container(obj, path):
    keys = path.split(".")
    for k in keys[:-1]:
        obj = obj[k]
    return obj, keys[-1]


def summary(s: dict) -> str:
    lines = [f"Auto: {s.get('carName', '?')}"]
    t = _get(s, K.P_PRESS)
    if t:
        lines.append("Reifendruck: " + ", ".join(f"{w} {K.PSI_BASE + K.PSI_PER_CLICK * v:.1f} psi" for w, v in zip(K.WHEEL_DE, t)))
    for label, path in (("Sturz-Clicks", K.P_CAMBER), ("Bremsbalance-Click", K.P_BIAS), ("Stabi vorne/hinten", None),
                        ("Heckflügel", K.P_WING), ("Diff-Preload", K.P_PRELOAD), ("ABS", K.P_ABS), ("TC1", K.P_TC),
                        ("Bremsbelüftung v/h", K.P_DUCT), ("Sprit [l]", K.P_FUEL)):
        if path is None:
            v = (_get(s, K.P_ARB_F), _get(s, K.P_ARB_R))
            if v[0] is not None:
                lines.append(f"{label}: {v[0]} / {v[1]}")
            continue
        v = _get(s, path)
        if v is not None:
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def collect_changes(res, max_packages: int = MAX_PACKAGES):
    """Übersetzt die Befunde in konkrete Setup-Änderungen.

    Immer: Reifendruck, Sturz, Bremsbelüftung (datengetrieben). Dazu die wichtigsten Fix-Pakete (max. 2).
    Rückgabe: (changes, applied_packages, skipped_packages)
    """
    changes = []
    c = res.clicks
    for i, d in enumerate(c.get("pressure") or []):
        if d:
            changes.append(dict(p=K.P_PRESS, i=i, d=d, label=f"Reifendruck {K.WHEEL_DE[i]} ({d * K.PSI_PER_CLICK:+.1f} psi)"))
    for i, d in enumerate(c.get("camber") or []):
        if d:
            changes.append(dict(p=K.P_CAMBER, i=i, d=d, label=f"Sturz {K.WHEEL_DE[i]}"))
    for i, d in enumerate(c.get("duct") or []):
        if d:
            changes.append(dict(p=K.P_DUCT, i=i, d=d, label=f"Bremsbelüftung {'vorne' if i == 0 else 'hinten'} weiter öffnen"))

    rank = {"hoch": 0, "mittel": 1, "niedrig": 2}
    cands = sorted((f for f in res.findings if f.level == "action" and f.fix), key=lambda f: rank.get(f.confidence, 1))
    applied, skipped, seen = [], [], set()
    for f in cands:
        if f.fix in seen:
            continue
        seen.add(f.fix)
        if len(applied) < max_packages:
            applied.append(f)
            changes += [dict(c_, label=c_["label"]) for c_ in K.FIXES[f.fix]["changes"]]
        else:
            skipped.append(f)
    return changes, applied, skipped


def plan_changes(setup: dict, changes: list) -> tuple[list[dict], list[str]]:
    """Fasst Änderungen je (Pfad, Index) zusammen und berechnet alt → neu.

    Gleiche (Pfad, Index): gleiche Richtung -> größere Änderung, sonst Summe; Werte >= 0.
    Rückgabe: (items, hinweise). Ein item: dict(p, i, d, label, old, new, text).
    """
    merged: dict = {}
    for ch in changes:
        key = (ch["p"], ch.get("i"))
        cur = merged.setdefault(key, dict(ch, d=0))
        if cur["d"] * ch["d"] > 0:      # gleiche Richtung von zwei Paketen: nur die größere Änderung
            cur["d"] = max(cur["d"], ch["d"]) if ch["d"] > 0 else min(cur["d"], ch["d"])
        else:                            # entgegengesetzt oder erste Änderung: summieren
            cur["d"] += ch["d"]
    items, notes = [], []
    for (path, idx), ch in merged.items():
        if ch["d"] == 0:
            continue
        cur = _get(setup, path)
        if cur is None:
            notes.append(f"übersprungen (Feld fehlt in dieser Setup-Datei): {path}")
            continue
        if isinstance(cur, list):
            if idx is None or idx >= len(cur):
                continue
            cur = cur[idx]
        old, nv = cur, max(0, cur + ch["d"])
        if nv == old:
            continue
        extra = f" ({K.PSI_BASE + K.PSI_PER_CLICK * old:.1f} → {K.PSI_BASE + K.PSI_PER_CLICK * nv:.1f} psi)" if path == K.P_PRESS else ""
        items.append(dict(p=path, i=idx, d=nv - old, label=ch["label"], old=old, new=nv,
                          text=f"{ch['label']}: {old} → {nv}{extra}"))
    return items, notes


def apply_items(setup: dict, items: list[dict]) -> dict:
    """Setzt die geplanten Werte in einer Kopie des Setups."""
    new = copy.deepcopy(setup)
    for it in items:
        parent, key = _set_container(new, it["p"])
        if isinstance(parent[key], list):
            parent[key][it["i"]] = it["new"]
        else:
            parent[key] = it["new"]
    return new


def apply_changes(setup: dict, changes: list) -> tuple[dict, list[str]]:
    """Wendet alle Änderungen auf eine Kopie an (siehe plan_changes)."""
    items, notes = plan_changes(setup, changes)
    return apply_items(setup, items), notes + [it["text"] for it in items]


FUEL_RESERVE_LAPS = 1.0   # Sicherheitsreserve
FORMATION_LAPS = 1.0      # Rennen: Formationsrunde (verbraucht Sprit, zählt nicht zur Renndauer)
INLAP_LAPS = 1.0          # Quali: zurück in die Box nach der Zielflagge
QUICK_LAPS = 3            # Quali "kurz": so viele schnelle Runden
TEMP_RE = re.compile(r"(\d{1,2})\s*C[ _\-]+(\d{1,2})\s*C", re.I)


def setup_temps(name: str) -> tuple[int, int] | None:
    """(Luft, Asphalt) aus einem Setup-Namen wie '20C_24C_40F' – None, wenn nicht enthalten."""
    m = TEMP_RE.search(Path(str(name)).stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _lt(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:04.1f}"


def fuel_variants(setup: dict, res, mode: str, cond: dict | None) -> list[dict]:
    """Spritmenge je Export-Variante. Rennen: Renndauer; Quali: volle Quali-Dauer und 'kurz' (wenige schnelle Runden).

    Rückgabe: Liste dict(key, label, item (Änderung oder None), fuel (Liter danach), txt).
    """
    c = cond or {}
    cur = _get(setup, K.P_FUEL)
    per_lap, per_src = c.get("fuel_per_lap"), "gemessen im Live-Mitschnitt"
    if not per_lap:
        per_lap, per_src = _get(setup, K.P_FUEL_PER_LAP), "Schätzung von ACC im Setup"
    lap = res.stats.get("lap_ref")

    def make(key, label, laps, why):
        if laps is None or not per_lap or cur is None:
            return dict(key=key, label=label, item=None, fuel=cur, txt=f"Sprit unverändert {cur} l ({why}).")
        need = max(2, int(math.ceil(laps * per_lap)))
        items, _ = plan_changes(setup, [dict(p=K.P_FUEL, i=None, d=need - cur, label=f"Sprit ({label})")])
        txt = (f"Sprit {cur} → {need} l: {why} = {laps:.0f} Runden × {per_lap:.2f} l/Runde ({per_src})." if items else
               f"Sprit {cur} l passt: {why} = {laps:.0f} Runden × {per_lap:.2f} l/Runde ({per_src}).")
        return dict(key=key, label=label, item=items[0] if items else None, fuel=need if items else cur, txt=txt)

    T = c.get("minutes")
    if mode == "race":
        if not T or not lap:
            return [make("race", "Rennen", None, "Renndauer unbekannt – Live-Mitschnitt oder selbst eintragen")]
        n = math.ceil(T * 60 / lap)
        return [make("race", f"Rennen {T} min", n + FORMATION_LAPS + FUEL_RESERVE_LAPS,
                     f"{T} min bei Ø {_lt(lap)} → {n} Runden + Formationsrunde + {FUEL_RESERVE_LAPS:.0f} Reserve")]
    if mode == "quali":
        q = c.get("quick") or QUICK_LAPS
        out = []
        if T and lap:
            n = math.ceil(T * 60 / lap)
            out.append(make("quali", f"Quali {T} min", n + INLAP_LAPS + FUEL_RESERVE_LAPS,
                            f"{T} min bei Ø {_lt(lap)} → {n} Runden inkl. Outlap + Inlap + {FUEL_RESERVE_LAPS:.0f} Reserve"))
        else:
            out.append(make("quali", "Quali", None, "Quali-Dauer unbekannt – Live-Mitschnitt oder selbst eintragen"))
        out.append(make("quali_kurz", f"Quali {q} schnelle Runden", q + 2 + FUEL_RESERVE_LAPS,
                        f"Outlap + {q} schnelle Runden + Inlap + {FUEL_RESERVE_LAPS:.0f} Reserve"))
        return out
    return [make("all", "Auswertung", None, "bei gemischter Auswahl keine Spritplanung")]


def temp_note(setup_path, cond: dict | None) -> str | None:
    """Hinweis, wenn das geladene Setup laut Namen für andere Temperaturen gedacht ist als gefahren."""
    st = setup_temps(setup_path) if setup_path else None
    c = cond or {}
    if not st or c.get("air") is None:
        return None
    da = c["air"] - st[0]
    dr = (c["road"] - st[1]) if c.get("road") is not None else 0
    if abs(da) < 3 and abs(dr) < 4:
        return None
    road = f" / Asphalt {c['road']:.0f} °C" if c.get("road") is not None else ""
    return (f"Dein Setup „{Path(str(setup_path)).stem}“ ist laut Name für {st[0]} °C Luft / {st[1]} °C Asphalt gedacht, "
            f"gefahren wurde bei Luft {c['air']:.0f} °C{road}. Die Druck-Empfehlung beruht auf den gemessenen Drücken und "
            "gleicht das bereits aus – die neue Datei bekommt die tatsächlichen Temperaturen im Namen.")


def plan_for(setup: dict, res, mode: str = "race", cond: dict | None = None, setup_path=None) -> dict:
    """Kompletter Änderungsplan für eine Auswertung: gemeinsame Änderungen (Druck usw.) + Sprit je Export-Variante."""
    changes, applied, skipped = collect_changes(res)
    items, notes = plan_changes(setup, changes)
    tn = temp_note(setup_path, cond)
    if tn:
        notes.append(tn)
    return dict(items=items, notes=notes, applied=applied, skipped=skipped,
                variants=fuel_variants(setup, res, mode, cond))


def plan_log(plan: dict, names: dict | None = None) -> list[str]:
    """Textzeilen für den Report. names: {variant_key: Dateiname}."""
    log = [it["text"] for it in plan["items"]] or ["Keine Änderung an Druck/Fahrwerk nötig."]
    log += plan["notes"]
    if plan["applied"]:
        log.append("Enthaltene Fix-Pakete: " + ", ".join(f"{f.title} → {K.FIXES[f.fix]['name']}" for f in plan["applied"]))
    log += [f"Nicht angewendet (erst die ersten Änderungen testen): {f.title}" for f in plan["skipped"]]
    for v in plan.get("variants") or []:
        n = (names or {}).get(v["key"])
        log.append(f"**{v['label']}**" + (f" → `{n}`" if n else "") + f": {v['txt']}")
    return log


def check_car(setup: dict, meta: dict) -> str | None:
    """Warnung, falls das Setup zu einem anderen Auto gehört als die Aufnahme."""
    a, b = setup.get("carName"), meta.get("car")
    if a and b and b != "?" and a.lower() != b.lower():
        return f"Achtung: Setup ist für „{a}“, die Aufnahme aber mit „{b}“ gefahren."
    return None


def _acc_scalar(v) -> str:
    if isinstance(v, float):
        return format(v, ".17g")      # ACC schreibt 17 signifikante Stellen (3.2407083511352539)
    return json.dumps(v)


def _acc_value(v, ind: int) -> list[str]:
    """Zeilen im ACC-Format: Tabs, Objekt-Klammer auf eigener Zeile, Zahlen-Arrays einzeilig."""
    t = "\t" * ind
    if isinstance(v, dict):
        out = [t + "{"]
        items = list(v.items())
        for n, (k, x) in enumerate(items):
            comma = "," if n < len(items) - 1 else ""
            key = t + "\t" + json.dumps(k) + ":"
            if isinstance(x, dict):
                out += [key] + _acc_value(x, ind + 1)
            elif isinstance(x, list) and any(isinstance(e, (dict, list)) for e in x):
                out += [key + " ["]
                for m, e in enumerate(x):
                    sub = _acc_value(e, ind + 2)
                    if m < len(x) - 1:
                        sub[-1] += ","
                    out += sub
                out += [t + "\t]"]
            else:
                out += [key + " " + _acc_value(x, 0)[0]]
            out[-1] += comma
        return out + [t + "}"]
    if isinstance(v, list):
        if not v:
            return [t + "[]"]
        if any(isinstance(e, (dict, list)) for e in v):
            out = [t + "["]
            for m, e in enumerate(v):
                sub = _acc_value(e, ind + 1)
                if m < len(v) - 1:
                    sub[-1] += ","
                out += sub
            return out + [t + "]"]
        return [t + "[ " + ", ".join(_acc_scalar(e) for e in v) + " ]"]
    return [t + _acc_scalar(v)]


def dumps(s: dict) -> str:
    return "\r\n".join(_acc_value(s, 0))


def save(s: dict, path: str):
    """Schreibt im selben Format wie ACC (Tabs, CRLF, 17-stellige Floats) – Diff zum Original = nur die Änderungen."""
    Path(path).write_bytes(dumps(s).encode("utf-8"))
