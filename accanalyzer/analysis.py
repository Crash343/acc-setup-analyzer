"""Telemetrie-Analyse und Setup-Empfehlungen.

Ausgelegt für ACC-GT3. Alle Schwellwerte sind Heuristiken (siehe TARGET) und lassen sich dort anpassen.
Die Empfehlungen sind Startpunkte zum Gegenprüfen, keine Garantie.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import knowledge as K
from .schema import WHEELS

# Zielfenster für ACC-GT3 (heiß gemessen, Trockenreifen). Hier anpassen.
TARGET = dict(p_hot=(26.0, 27.0), p_target=26.8, core=(75, 95), brake=(350, 750), cam_spread=(3, 11))
# p_target: Ziel-Heißdruck (psi). Der Ø über alle gültigen Runden wird darauf korrigiert.
PSI_PER_CLICK = K.PSI_PER_CLICK
P_DEADBAND = 0.1   # psi – Abweichungen bis hierhin gelten als "passt"
OUTLIER_PSI = 1.0  # psi – Session weicht so stark vom Median der übrigen ab -> nicht im Druck-Ø (ab 3 Sessions)
CAMBER_DEG_PER_CLICK = 0.1
N_SEGMENTS = 20


@dataclass
class Finding:
    area: str            # Reifen, Bremse, Balance, Traktion, Fahrer, Treibstoff
    level: str           # ok | info | warn | action
    title: str
    detail: str = ""
    actions: list = field(default_factory=list)
    confidence: str = "mittel"
    fix: str | None = None   # ID aus knowledge.FIXES -> wird für --setup in Clicks übersetzt


@dataclass
class Result:
    laps: pd.DataFrame
    findings: list
    segments: pd.DataFrame | None
    clicks: dict           # {"pressure":[fl,fr,rl,rr], "camber":[...]} Vorschläge in Clicks
    stats: dict


# ----------------------------------------------------------------------------- Vorbereitung
def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["v"] = df["speed"] / 3.6
    free = (df.brake < 0.02) & (df.g_lon.abs() < 0.15) & (df.g_lat.abs() < 0.3) & (df.speed > 80)
    for w in WHEELS:
        ok = free & (df[f"w_{w}"] > 1)
        if ok.sum() > 50:
            r = (df.v[ok] / df[f"w_{w}"][ok]).median()
            df[f"sr_{w}"] = df[f"w_{w}"] * r / df.v.where(df.v > 5) - 1.0
        else:
            df[f"sr_{w}"] = np.nan
    return df


def lap_table(df: pd.DataFrame, meta: dict | None = None) -> pd.DataFrame:
    """Eine Zeile pro Runde. valid = sauber gefahrene volle Runde (clean) UND in der Auswahl (wird in analyze gesetzt)."""
    if meta and meta.get("laps_info"):  # LD-Import: Runden/Sessions stammen aus Beacons und Stillständen
        rows = []
        for l in meta["laps_info"]:
            g = df[df.lap == l["lap"]]
            if g.empty:
                continue
            rows.append(dict(lap=l["lap"], session=l["session"], num=l["num"], kind=l["kind"], n=len(g),
                             t0=l["start"], t1=l["end"], cover=1.0, pit=False, lap_s=l["dur"],
                             fuel_start=g.fuel.iloc[0], fuel_end=g.fuel.iloc[-1], valid=l["valid"], dist=l["dist"]))
        laps = pd.DataFrame(rows)
    else:
        rows = []
        for lap, g in df[df.lap >= 0].groupby("lap"):
            cover = g.pos.max() - g.pos.min()
            rows.append(dict(
                lap=int(lap), session=0, num=int(lap), n=len(g), t0=g.t.iloc[0], t1=g.t.iloc[-1],
                cover=cover, pit=bool(g.pit.max() > 0), lap_s=g.lap_ms.max() / 1000.0,
                fuel_start=g.fuel.iloc[0], fuel_end=g.fuel.iloc[-1],
            ))
        laps = pd.DataFrame(rows)
        laps["valid"] = (laps.cover > 0.97) & (~laps.pit) & (laps.lap_s > 20)
        laps["kind"] = np.where(laps.valid, "gezeitet", "unvollständig")
    laps["clean"] = laps.valid
    laps["used"] = True
    return laps


MODES = {"race": "Rennen", "quali": "Quali", "all": "Auswahl"}   # Auswertungsart -> Anzeigename


def _is_race_like(s: dict) -> bool:
    live = (s.get("live") or {}).get("type")
    if live in (1, 8):          # laut ACC (Live-Mitschnitt) eine Quali -> nie als Rennen werten
        return False
    if live == 2:
        return s["laps_valid"] >= 1
    return s["type"] == "Rennen" and s["laps_valid"] >= 3 or s["laps_valid"] >= MIN_RACE_STINT


def default_sessions(meta: dict | None) -> list[int] | None:
    """Standardauswahl: Rennen und Longruns (>= MIN_RACE_STINT gültige Runden); sonst alles (None)."""
    ss = (meta or {}).get("sessions") or []
    sel = [s["id"] for s in ss if _is_race_like(s)]
    return sel or None


def default_groups(meta: dict | None) -> dict[str, list[int] | None]:
    """Standard-Zuordnung der Sessions: Rennen/Longruns -> "race", übrige Sessions mit gültiger Runde -> "quali".

    Ohne Session-Info (Recorder-CSV, Demo) eine einzige Auswertung über alles ("all": None).
    """
    ss = (meta or {}).get("sessions") or []
    race = [s["id"] for s in ss if _is_race_like(s)]
    quali = [s["id"] for s in ss if s["id"] not in race and s["laps_valid"] >= 1]
    out: dict = {}
    if race:
        out["race"] = race
    if quali:
        out["quali"] = quali
    return out or {"all": None}


def session_table(df: pd.DataFrame, laps: pd.DataFrame, meta: dict | None) -> list[dict]:
    """Kennzahlen je Session (immer alle Sessions, unabhängig von der Auswahl)."""
    ss = (meta or {}).get("sessions") or [dict(id=0, type="Session", start=float(df.t.iloc[0]), end=float(df.t.iloc[-1]))]
    out = []
    for s in ss:
        L = laps[laps.session == s["id"]]
        c = L[L.clean]
        stints = get_stints(L, col="clean")
        # Rennen/Longrun: 1. Runde jedes Stints (Reifen noch kalt) weglassen; Quali: Outlap hat die Reifen schon angewärmt
        skip_first = s["type"] == "Rennen" or len(c) >= MIN_RACE_STINT
        warm = [l for st in stints for l in (st[1:] if skip_first and len(st) >= 2 else st)]
        d = df[df.lap.isin(warm) & (df.speed > 80)]
        p = {w: float(d.groupby("lap")[f"p_{w}"].mean().mean()) if len(d) else float("nan") for w in WHEELS}
        tc = {w: float(d[f"tc_{w}"].mean()) if len(d) else float("nan") for w in WHEELS}
        out.append(dict(id=s["id"], type=s["type"], start=s["start"], dur=s["end"] - s["start"], laps=len(L), laps_valid=len(c),
                        best=float(c.lap_s.min()) if len(c) else float("nan"),
                        median=float(c.lap_s.median()) if len(c) else float("nan"),
                        p=p, tc=tc, used=bool(L.used.any()) if len(L) else False))
    return out


# ----------------------------------------------------------------------------- Hilfsfunktionen
def _fmt_clicks(n: int) -> str:
    return f"{n:+d} Click{'s' if abs(n) != 1 else ''}"


def _axle(df, mask, cols):
    return df.loc[mask, cols].abs().mean().mean()


# ----------------------------------------------------------------------------- Teilanalysen
MIN_RACE_STINT = 6   # ab so vielen gültigen Runden am Stück gilt ein Stint als Rennstint


def get_stints(laps: pd.DataFrame, col: str = "valid") -> list[list[int]]:
    """Aufeinanderfolgende gültige Runden derselben Session; jede ungültige Runde (Boxenstopp, Abflug, Neustart)
    und jeder Session-Wechsel trennt Stints."""
    stints, cur, last_s = [], [], None
    for r in laps.sort_values("lap").itertuples():
        s = getattr(r, "session", 0)
        if cur and s != last_s:
            stints.append(cur)
            cur = []
        last_s = s
        if getattr(r, col):
            cur.append(int(r.lap))
        elif cur:
            stints.append(cur)
            cur = []
    if cur:
        stints.append(cur)
    return stints


def stint_table(df, laps, stints) -> list[dict]:
    rows = []
    lt = laps.set_index("lap").lap_s
    for st in stints:
        if len(st) < 3:
            continue
        d = df[df.lap.isin(st) & (df.speed > 80)]
        early, late = st[1:3], st[-2:]
        pe = d[d.lap.isin(early)][[f"p_{w}" for w in WHEELS]].mean()
        pl = d[d.lap.isin(late)][[f"p_{w}" for w in WHEELS]].mean()
        times = lt.loc[st].values
        clean = times[times <= np.min(times) * 1.05]
        slope = np.polyfit(np.arange(len(clean)), clean, 1)[0] if len(clean) >= 5 else np.nan
        r0 = laps.set_index("lap").loc[st[0]]
        rows.append(dict(first=st[0], last=st[-1], n=len(st), session=int(r0.get("session", 0)),
                         num_first=int(r0.get("num", st[0])), num_last=int(laps.set_index("lap").loc[st[-1]].get("num", st[-1])), lap_median=float(np.median(times)), lap_best=float(times.min()),
                         p_early=[float(x) for x in pe], p_late=[float(x) for x in pl],
                         temp_late=float(d[d.lap.isin(late)][[f"tc_{w}" for w in WHEELS]].mean().mean()),
                         slope=float(slope)))
    return rows


def analyze_tyres(df, laps, tg, clicks, compound, stats_out: dict, mode: str = "race") -> list:
    out = []
    valid = laps[laps.valid]
    if len(valid) < 1:
        return [Finding("Reifen", "info", "Keine gültige Runde",
                        "Mindestens 3 saubere Runden am Stück für belastbare Reifendaten fahren.")]
    stints = get_stints(laps)
    race = [st for st in stints if len(st) >= MIN_RACE_STINT] if mode != "quali" else []
    if race:   # Stint-Auswertung: Druck am Stintbeginn (Runde 2–3) und am Stintende (letzte 2 Runden)
        early_laps = sorted({l for st in race for l in st[1:3]})
        late_laps = sorted({l for st in race for l in st[-2:]})
        use = sorted(set(early_laps) | set(late_laps))
        pe = {w: np.mean([df[df.lap.isin(st[1:3]) & (df.speed > 80)][f"p_{w}"].mean() for st in race]) for w in WHEELS}
        pl = {w: np.mean([df[df.lap.isin(st[-2:]) & (df.speed > 80)][f"p_{w}"].mean() for st in race]) for w in WHEELS}
        basis = f"{len(race)} Rennstint(s) à ≥{MIN_RACE_STINT} Runden"
    elif mode == "quali":   # alle gezeiteten Quali-Runden (die Outlap hat die Reifen angewärmt)
        use = valid.lap.tolist()
        dd = df[df.lap.isin(use) & (df.speed > 80)]
        pe = pl = {w: dd[f"p_{w}"].mean() for w in WHEELS}
        basis = f"{len(use)} Quali-Runde(n)"
    else:
        use = valid.lap.tolist()[-3:]
        dd = df[df.lap.isin(use) & (df.speed > 80)]
        pe = pl = {w: dd[f"p_{w}"].mean() for w in WHEELS}
        basis = f"letzte {len(use)} Runde(n)"
    d = df[df.lap.isin(use) & (df.speed > 80)]

    wet = isinstance(compound, str) and "wet" in compound.lower()
    lo, hi = tg["p_hot"]
    target = tg["p_target"]
    TOL = 0.15
    # Ø-Heißdruck über alle gültigen Runden; im Rennen zählt die 1. Runde jedes Stints (Reifen noch kalt) nicht mit,
    # in der Quali schon (davor liegt immer eine Outlap)
    avg_laps = [l for st in stints for l in (st if mode == "quali" else st[1:] if len(st) >= 2 else st)]

    # Ø je Session; eine Session, die stark vom Rest abweicht (Reifen kalt, andere Kaltdrücke), fliegt aus dem Ø
    lap_sess = laps.set_index("lap").session if "session" in laps else None
    per = {}
    if lap_sess is not None:
        for sid in sorted({int(lap_sess[l]) for l in avg_laps}):
            ds = df[df.lap.isin([l for l in avg_laps if lap_sess[l] == sid]) & (df.speed > 80)]
            per[sid] = {w: float(ds.groupby("lap")[f"p_{w}"].mean().mean()) for w in WHEELS}
    excluded = []
    if len(per) >= 3:
        mean = {sid: np.nanmean([v[w] for w in WHEELS]) for sid, v in per.items()}
        med = float(np.median(list(mean.values())))
        excluded = [sid for sid, m in mean.items() if abs(m - med) > OUTLIER_PSI]
        if len(excluded) < len(per):
            avg_laps = [l for l in avg_laps if lap_sess[l] not in excluded]
        else:
            excluded = []
    da = df[df.lap.isin(avg_laps) & (df.speed > 80)]
    avg = {w: float(da.groupby("lap")[f"p_{w}"].mean().mean()) for w in WHEELS}
    diff = {w: avg[w] - target for w in WHEELS}
    # kalt ≈ 1:1 heiß; innerhalb ±P_DEADBAND keine Änderung (Streuung zwischen Sessions ist größer als 1 Click)
    pdelta = {w: 0 if np.isnan(diff[w]) or abs(diff[w]) <= P_DEADBAND else int(round(-diff[w] / PSI_PER_CLICK)) for w in WHEELS}
    stats_out["pressure"] = dict(target=target, n_laps=len(avg_laps), avg=avg, diff=diff, clicks=pdelta, wet=wet,
                                 first_lap_excluded=mode != "quali", excluded_sessions=excluded)
    # Ø-Temperaturen über dieselben Runden wie der Druck
    stats_out["temps"] = dict(
        tyre={w: float(da[f"tc_{w}"].mean()) for w in WHEELS},
        brake_peak={w: float(df[df.lap.isin(avg_laps)].groupby("lap")[f"bt_{w}"].max().mean()) for w in WHEELS})

    txt = ", ".join(f"{w.upper()} {pe[w]:.1f}→{pl[w]:.1f}" for w in WHEELS) if race else ", ".join(f"{w.upper()} {pe[w]:.1f}" for w in WHEELS)
    what = "Stintbeginn→Stintende" if race else "heiß"
    avg_txt = ", ".join(f"{w.upper()} {avg[w]:.2f} ({diff[w]:+.2f})" for w in WHEELS)
    if wet:
        out.append(Finding("Reifen", "info", "Regenreifen: Druckziel nicht bewertet", f"Ø Heißdrücke: {avg_txt} psi"))
    else:
        bad = [w for w in WHEELS if pdelta[w] != 0]
        if bad:
            clicks["pressure"] = [pdelta[w] for w in WHEELS]
            acts = [f"{w.upper()}: {_fmt_clicks(pdelta[w])} ({pdelta[w] * PSI_PER_CLICK:+.1f} psi kalt)" for w in bad]
            out.append(Finding("Reifen", "action", f"Ø Heißdruck weicht von {target} psi ab",
                               f"Ø über {len(avg_laps)} Runde(n), Abweichung zum Ziel in Klammern: {avg_txt} psi. "
                               + (f"Verlauf ({basis}; {what}): {txt} psi." if mode != "quali" else ""), acts, "mittel"))
        else:
            out.append(Finding("Reifen", "ok", f"Ø Heißdrücke auf Ziel {target} psi", f"Ø über {len(avg_laps)} Runde(n): {avg_txt} psi."))
        if race:
            peak = d.groupby("lap")[[f"p_{w}" for w in WHEELS]].mean().max()
            if peak.max() > hi + TOL:
                out.append(Finding("Reifen", "info", f"Spitzenwert im Rennstint {peak.max():.1f} psi",
                                   "Höchster Rundenmittelwert pro Rad: " + ", ".join(f"{w.upper()} {peak[f'p_{w}']:.1f}" for w in WHEELS)
                                   + f". Bei wärmerer Strecke oder längerem Stint kann das Fenster ({hi} psi) überschritten werden – dann 1–2 Clicks weniger kalt."))

    # Streuung des Ø-Drucks zwischen den ausgewerteten Sessions (Strecken-/Lufttemperatur, Sprit, Verkehr)
    if len(per) >= 2:
        spread = {w: max(v[w] for v in per.values()) - min(v[w] for v in per.values()) for w in WHEELS}
        if max(spread.values()) > 0.3 or excluded:
            ex = ""
            if excluded:
                names = ", ".join(f"S{x + 1}" for x in excluded)
                ex = (f" {names} weicht um mehr als {OUTLIER_PSI:.0f} psi von den übrigen Sessions ab und ist NICHT im Ø "
                      "(Reifen noch kalt oder andere Kaltdrücke gefahren?). Zum Prüfen diese Session alleine auswerten (im Fenster nur sie auf Quali/Rennen stellen).")
            out.append(Finding("Reifen", "warn" if excluded else "info",
                               f"Ø Druck schwankt zwischen den Sessions um bis zu {max(spread.values()):.1f} psi",
                               "Ø je Session (VL/VR/HL/HR): " + "; ".join(
                                   f"S{sid + 1}: " + " / ".join(f"{per[sid][w]:.2f}" for w in WHEELS) for sid in per)
                               + ". Ursache meist Strecken-/Lufttemperatur oder Verkehr (Windschatten heizt die Reifen)." + ex
                               + " Bei deutlich wärmerer/kälterer Strecke 1–2 Clicks nachjustieren."))

    # Kerntemperatur
    clo, chi = tg["core"]
    core = stats_out["temps"]["tyre"]
    ct = ", ".join(f"{w.upper()} {core[w]:.0f}" for w in WHEELS)
    hot = [w for w in WHEELS if core[w] > chi + 3]
    cold = [w for w in WHEELS if core[w] < clo - 3]
    if hot:
        out.append(Finding("Reifen", "warn", f"Reifen überhitzt (> {chi + 3} °C)", f"Reifentemperaturen: {ct} °C.",
                           K.SYMPTOM_CHECKS["hotPressures"]))
    elif cold:
        out.append(Finding("Reifen", "warn", f"Reifen zu kalt (< {clo - 3} °C)", f"Reifentemperaturen: {ct} °C.",
                           K.SYMPTOM_CHECKS["coldTyres"] + ["Prüfen, ob genug Runden am Limit gefahren wurden (Aufwärmphase)."]))
    else:
        out.append(Finding("Reifen", "ok", "Reifentemperaturen im Fenster", f"{ct} °C (Ziel {clo}–{chi})."))

    # Sturz über Innen-/Außentemperatur (nur wenn I/M/O befüllt sind)
    corner = d[d.g_lat.abs() > 0.8]
    have_imo = all(df[f"ti_{w}"].abs().max() > 1 for w in WHEELS)
    if have_imo and len(corner) > 200:
        slo, shi = tg["cam_spread"]
        cdelta = {}
        det = []
        for w in WHEELS:
            spread = (corner[f"ti_{w}"] - corner[f"to_{w}"]).mean()
            det.append(f"{w.upper()} {spread:+.1f}")
            if spread > shi + 2:
                cdelta[w] = +2 if spread > shi + 7 else +1  # weniger negativ
            elif spread < slo - 2:
                cdelta[w] = -2 if spread < slo - 7 else -1  # negativer
            else:
                cdelta[w] = 0
        if any(cdelta.values()):
            clicks["camber"] = [cdelta[w] for w in WHEELS]
            acts = []
            for w in WHEELS:
                if cdelta[w] > 0:
                    acts.append(f"{w.upper()}: Sturz weniger negativ ({cdelta[w] * CAMBER_DEG_PER_CLICK:.1f}°)")
                elif cdelta[w] < 0:
                    acts.append(f"{w.upper()}: Sturz mehr negativ ({-cdelta[w] * CAMBER_DEG_PER_CLICK:.1f}°)")
            out.append(Finding("Reifen", "action", "Sturz: Innen-/Außen-Spreizung nicht optimal",
                               f"Mittlere Spreizung innen−außen in Kurven (°C): {', '.join(det)}; Ziel {slo}–{shi}.",
                               acts, "mittel"))
        else:
            out.append(Finding("Reifen", "ok", "Sturz passt (Innen/Außen-Spreizung)", f"Spreizung innen−außen (°C): {', '.join(det)}."))
    elif not have_imo:
        out.append(Finding("Reifen", "info", "Keine Innen/Mitte/Außen-Reifentemperaturen in den Daten",
                           "Der MoTeC-Export enthält nur eine Reifentemperatur pro Rad (TYRE_TAIR) – die Sturz-Empfehlung entfällt. "
                           "Alternativ im Spiel das Reifen-HUD beobachten oder die Live-Aufnahme (Shared Memory) nutzen."))
    return out


def analyze_brakes(df, laps, tg, clicks) -> list:
    out = []
    valid = laps[laps.valid]
    if valid.empty:
        return out
    peak = {w: df[df.lap.isin(valid.lap)].groupby("lap")[f"bt_{w}"].max().mean() for w in WHEELS}
    txt = ", ".join(f"{w.upper()} {peak[w]:.0f}" for w in WHEELS)
    lo, hi = tg["brake"]
    f_peak = (peak["fl"] + peak["fr"]) / 2
    r_peak = (peak["rl"] + peak["rr"]) / 2
    if max(peak.values()) > hi + 50:
        duct = [1 if f_peak > hi + 50 else 0, 1 if r_peak > hi + 50 else 0]
        clicks["duct"] = duct
        ax = " und ".join(n for n, d in zip(("vorne", "hinten"), duct) if d)
        out.append(Finding("Bremse", "action", f"Bremsen zu heiß (Spitze > {hi + 50} °C)", f"Ø Spitzentemperaturen pro Runde: {txt} °C.",
                           [f"Bremsbelüftung {ax} um 1 Stufe weiter öffnen (+1 Click).",
                            "Bei weiter hohen Werten: Bremspunkte und Bremsbalance prüfen."], "mittel"))
    elif max(peak.values()) > hi:
        out.append(Finding("Bremse", "warn", "Bremsen an der oberen Grenze", f"Ø Spitzentemperaturen: {txt} °C.",
                           ["Beobachten; bei Rennlänge > 30 min Brake Duct 1 Stufe weiter öffnen."]))
    elif min(peak.values()) < lo - 50:
        out.append(Finding("Bremse", "info", f"Bremsen teils zu kalt (Spitze < {lo - 50} °C)", f"Ø Spitzentemperaturen: {txt} °C.",
                           ["Brake Duct 1 Stufe schließen (bessere Anfangsbiss/Verschleiß)."]))
    else:
        out.append(Finding("Bremse", "ok", "Bremsentemperaturen im Fenster", f"Ø Spitzentemperaturen: {txt} °C."))

    # Blockieren / ABS
    if df["sr_fl"].notna().any():
        brk = (df.brake > 0.25) & (df.speed > 80) & df.lap.isin(valid.lap)
        if brk.sum() > 100:
            front = ((df.sr_fl < -0.15) | (df.sr_fr < -0.15))[brk].mean()
            rear = ((df.sr_rl < -0.15) | (df.sr_rr < -0.15))[brk].mean()
            d = f"Anteil Bremszeit mit Radschlupf < −15 %: vorne {front * 100:.1f} %, hinten {rear * 100:.1f} %."
            if front > 0.06 and front > 2 * rear:
                out.append(Finding("Bremse", "action", "Vorderachse blockiert/ABS-Eingriffe vorne", d,
                                   K.describe("fxFrontLock") + K.SYMPTOM_CHECKS["frontLock"]
                                   + ["Fahrer: Bremsdruck-Spitze reduzieren, dann halten."], "mittel", fix="fxFrontLock"))
            elif rear > 0.04 and rear > 1.5 * front:
                out.append(Finding("Bremse", "action", "Hinterachse blockiert/instabil beim Bremsen", d,
                                   K.describe("fxRearLock") + ["Falls es nur in schnellen Kurven passiert: Heckflügel +1."], "mittel", fix="fxRearLock"))
            else:
                out.append(Finding("Bremse", "ok", "Bremsbalance unauffällig", d))
    return out


def analyze_traction(df, laps) -> list:
    valid = laps[laps.valid]
    if valid.empty or not df["sr_rl"].notna().any():
        return []
    m = (df.gas > 0.6) & (df.speed < 170) & (df.speed > 40) & df.lap.isin(valid.lap)
    if m.sum() < 100:
        return []
    spin = ((df.sr_rl > 0.12) | (df.sr_rr > 0.12))[m].mean()
    tc_act = (df.tcs[m] > 0.01).mean()
    d = f"Heck-Wheelspin (>12 % Schlupf) in {spin * 100:.1f} % der Vollgaszeit unter 170 km/h; TC greift in {tc_act * 100:.0f} %."
    if spin > 0.08:
        return [Finding("Traktion", "action", "Zu viel Wheelspin beim Beschleunigen", d,
                        K.describe("fxTraction") + K.SYMPTOM_CHECKS["exitSpin"], "mittel", fix="fxTraction")]
    if tc_act > 0.5 and spin < 0.03:
        return [Finding("Traktion", "info", "TC greift sehr oft ein", d,
                        ["TC um 1 Stufe senken – Leistung wird verschenkt, solange kaum Wheelspin entsteht.",
                         "Hinweis: TC maskiert Balance-Probleme; zum Lernen eine Stufe niedriger fahren als bequem."])]
    return [Finding("Traktion", "ok", "Traktion unauffällig", d)]


def analyze_balance(df, laps, tg) -> list:
    valid = laps[laps.valid]
    d0 = df[df.lap.isin(valid.lap) & (df.speed > 60)].copy()
    if len(d0) < 500:
        return []
    have_ws = d0[["ws_fl", "ws_fr", "ws_rl", "ws_rr"]].notna().all(axis=1).mean() > 0.5
    have_sr = d0[["sr_fl", "sr_fr", "sr_rl", "sr_rr"]].notna().all(axis=1).mean() > 0.5

    # Referenz-Lenkreaktion bei niedriger Querbeschleunigung (pro Geschwindigkeitsbin)
    steer_ref = d0.steer.abs().quantile(0.99)
    d0["k"] = d0.yaw.abs() / (d0.v * d0.steer.abs().clip(lower=1e-6))
    d0["vbin"] = (d0.speed // 25).astype(int)
    low = d0[(d0.g_lat.abs() < 0.6) & (d0.steer.abs() > 0.05 * steer_ref) & (d0.yaw.abs() > 0.02)]
    kref = low.groupby("vbin").k.median()
    kref = kref[low.groupby("vbin").size() > 10]

    cornering = d0[d0.g_lat.abs() > 0.8]
    if len(cornering) < 200:
        return []
    prod = np.sign(cornering.steer) * np.sign(cornering.yaw)
    s0 = np.sign(prod.median()) or 1.0

    phases = {
        "Kurveneingang": (d0.brake > 0.05) & (d0.brake < 0.6) & (d0.g_lat.abs() > 0.6),
        "Kurvenmitte": (d0.brake < 0.05) & (d0.gas < 0.5) & (d0.g_lat.abs() > 1.0),
        "Kurvenausgang": (d0.gas > 0.5) & (d0.g_lat.abs() > 0.7) & (d0.speed < 200),
    }
    out = []
    for name, m in phases.items():
        if m.sum() < 80:
            continue
        sub = d0[m]
        us = os_ = 0
        det = []
        # (a) Schlupf-Verhältnis hinten/vorne (nur wenn wheelSlip verfügbar, d. h. Live-Aufnahme)
        if have_ws:
            front = sub[["ws_fl", "ws_fr"]].abs().mean().mean()
            rear = sub[["ws_rl", "ws_rr"]].abs().mean().mean()
            ratio = rear / front if front > 1e-6 else np.nan
            det.append(f"Schlupf hinten/vorne = {ratio:.2f}")
            us += int(ratio < 0.8); os_ += int(ratio > 1.25)
        # (b) Raddrehzahl-Differenz hinten−vorne (nur Eingang/Ausgang aussagekräftig)
        if have_sr and name != "Kurvenmitte":
            dsr = sub[["sr_rl", "sr_rr"]].mean().mean() - sub[["sr_fl", "sr_fr"]].mean().mean()
            det.append(f"Δ Radschlupf hinten−vorne = {dsr * 100:+.1f} %")
            if name == "Kurveneingang":
                us += int(dsr > 0.03); os_ += int(dsr < -0.03)
            else:
                os_ += int(dsr > 0.05)
        # (c) Lenkreaktion relativ zum Verhalten bei niedriger Querbeschleunigung
        kr = np.nan
        if len(kref):
            kk = (sub.k / sub.vbin.map(kref)).replace([np.inf, -np.inf], np.nan).dropna()
            kr = kk.median() if len(kk) > 30 else np.nan
        if not np.isnan(kr):
            det.append(f"Lenkreaktion vs. Referenz = {kr:.2f}")
            # Auf echten ACC-Daten liegt der neutrale Wert bei ca. 1.1 (Eingang bis ~1.3 durch Trail-Braking)
            us += int(kr < 0.85) + int(kr < 0.7)
            os_ += int(kr > 1.35) + int(kr > 1.6)
        # (d) Gegenlenken
        cs = sub[sub.yaw.abs() > 0.1]
        counter = float(((np.sign(cs.steer) * np.sign(cs.yaw)) != s0).mean()) if len(cs) else 0.0
        det.append(f"Gegenlenken-Anteil = {counter * 100:.0f} %")
        os_ += int(counter > 0.2)

        verdict = "us" if us >= 2 and us > os_ else "os" if os_ >= 2 and os_ > us else "neutral"
        txt = ", ".join(det) + f"  (n={int(m.sum())})"
        if verdict == "neutral":
            out.append(Finding("Balance", "ok", f"{name}: neutral", txt))
        else:
            out.append(_balance_finding(name, verdict, txt, us, os_))
    return out


BALANCE_FIX = {  # (Phase, Urteil) -> Fix-Paket (Setup Ripple) + Zusatzhinweise
    ("Kurveneingang", "us"): ("fxEntryUnder", []),
    ("Kurveneingang", "os"): ("fxEntrySnap", ["Passiert es nur in schnellen Kurven: Heckflügel +1 (fxFastOver).",
                                              "Hinterreifen zu kalt/zu wenig Druck? Erst Heißdrücke prüfen."]),
    ("Kurvenmitte", "us"): ("fxMidUnder", ["Nur in schnellen Kurven? Dann Aero: Heckflügel −1 oder Fahrhöhe vorne niedriger (fxFastUnder).",
                                          "Erst Heißdrücke vorne prüfen – zu hoher Druck verkleinert die Aufstandsfläche."]),
    ("Kurvenmitte", "os"): ("fxMidOver", ["Nur in schnellen Kurven? Dann Aero: Heckflügel +1 (fxFastOver).",
                                         "Etwas mehr Spur hinten (toe-in) beruhigt das Heck überall."]),
    ("Kurvenausgang", "us"): ("fxPowerPush", K.SYMPTOM_CHECKS["exitPush"]),
    ("Kurvenausgang", "os"): ("fxTraction", K.SYMPTOM_CHECKS["exitSpin"]),
}


def _balance_finding(phase, verdict, det, us, os_):
    conf = "hoch" if max(us, os_) >= 3 else "mittel"
    fix, extra = BALANCE_FIX[(phase, verdict)]
    word = "Untersteuern" if verdict == "us" else "Übersteuern"
    return Finding("Balance", "action", f"{phase}: {word}", det, K.describe(fix) + extra, conf, fix=fix)


def analyze_bumpstops(df, laps) -> list:
    """Anteil der Fahrzeit auf den Bumpstops (nur im ACC-Export enthalten)."""
    cols = [f"bsu_{w}" for w in WHEELS] + [f"bsd_{w}" for w in WHEELS]
    if not all(c in df.columns for c in cols) or df[cols].isna().all().all():
        return []
    valid = laps[laps.valid]
    d = df[df.lap.isin(valid.lap) & (df.speed > 60)]
    if len(d) < 500:
        return []
    out = []
    share = {w: float((d[f"bsu_{w}"] > 0.5).mean()) for w in WHEELS}
    txt = ", ".join(f"{w.upper()} {share[w] * 100:.1f} %" for w in WHEELS)
    front = (share["fl"] + share["fr"]) / 2
    rear = (share["rl"] + share["rr"]) / 2
    worst = max(front, rear)
    axle = "vorne" if front >= rear else "hinten"
    # GT3-Autos fahren mit Abtrieb oft auf den Bumpstops (Plattformkontrolle) -> nur extreme Werte sind ein Befund.
    if worst > 0.40:
        out.append(Finding("Fahrwerk", "warn", f"Bumpstop-Kontakt {axle} sehr häufig (> 40 % der Fahrzeit)", f"Anteil Fahrzeit auf dem oberen Bumpstop: {txt}.",
                           [f"Ride Height {axle} um 1–2 mm erhöhen oder Feder {axle} härter, damit der Federweg nicht ständig aufgebraucht ist.",
                            "Danach Balance prüfen (Rake verändert sich)."], "niedrig"))
    else:
        out.append(Finding("Fahrwerk", "info", f"Bumpstop-Kontakt (oberer Anschlag): {axle} am meisten", f"Anteil Fahrzeit: {txt}. "
                           "Bei GT3 mit hohem Abtrieb normal; nur eingreifen, wenn das Auto an Kerbs/Bodenwellen unruhig wird "
                           "(dann Ride Height +1 mm an der betroffenen Achse)."))
    dn = {w: float((d[f"bsd_{w}"] > 0.5).mean()) for w in WHEELS}
    if max(dn.values()) > 0.10:
        out.append(Finding("Fahrwerk", "info", "Unterer Anschlag (Ausfedern) wird öfter erreicht",
                           "Anteil Fahrzeit: " + ", ".join(f"{w.upper()} {dn[w] * 100:.1f} %" for w in WHEELS)))
    return out


def analyze_fuel(laps) -> list:
    v = laps[laps.valid]
    if len(v) < 2 or v.fuel_start.isna().all():
        return []
    per_lap = (v.fuel_start - v.fuel_end).clip(lower=0)
    c = per_lap.median()
    return [Finding("Treibstoff", "info", f"Verbrauch ≈ {c:.2f} l/Runde",
                    f"Median über {len(v)} gültige Runden. Pro 10 Runden ≈ {c * 10:.0f} l (+ Reserve einplanen).")]


def analyze_driver(df, laps):
    v = laps[laps.valid].copy()
    out, segs = [], None
    if len(v) < 2:
        return out, segs
    best = v.loc[v.lap_s.idxmin()]
    clean = v[v.lap_s <= 1.05 * best.lap_s]
    n_out = len(v) - len(clean)
    spread = clean.lap_s.std() if len(clean) >= 2 else float("nan")
    out.append(Finding("Fahrer", "info", f"Beste Runde {_laptime(best.lap_s)} ({lap_name(best)})",
                       f"Konstanz: σ = {spread:.2f} s über {len(clean)} saubere Runden (≤ 105 % der Bestzeit), "
                       f"Median {_laptime(clean.lap_s.median())}"
                       + (f"; {n_out} Runde(n) deutlich langsamer (z. B. Startrunde, Verkehr, Fehler) nicht eingerechnet." if n_out else ".")))
    # Mini-Sektoren
    edges = np.linspace(0, 1, N_SEGMENTS + 1)
    rows = []
    for lap in v.lap:
        g = df[df.lap == lap]
        pos = np.maximum.accumulate(g.pos.values)
        t = g.t.values
        tt = np.interp(edges[:-1], pos, t)
        tt = np.append(tt, t[-1])
        seg_t = np.diff(tt)
        vmin = [g.speed[(g.pos >= edges[i]) & (g.pos < edges[i + 1])].min() for i in range(N_SEGMENTS)]
        rows.append(pd.DataFrame(dict(lap=lap, seg=range(N_SEGMENTS), t=seg_t, vmin=vmin)))
    s = pd.concat(rows)
    piv = s.pivot(index="seg", columns="lap", values="t")
    best_seg = piv.min(axis=1)
    loss = piv.sub(best_seg, axis=0).mean(axis=1)
    vmin = s.groupby("seg").vmin
    segs = pd.DataFrame(dict(
        von=edges[:-1], bis=edges[1:], beste=best_seg, mittel_verlust=loss,
        vmin_streuung=vmin.max() - vmin.min()))
    top = segs.sort_values("mittel_verlust", ascending=False).head(3)
    theo = best_seg.sum()
    out.append(Finding("Fahrer", "info", f"Theoretisch beste Runde: {_laptime(theo)} (−{best.lap_s - theo:.2f} s)",
                       "Größte Zeitverluste pro Runde gegenüber deinen Bestzeiten je Streckenabschnitt: " +
                       "; ".join(f"{r.von * 100:.0f}–{r.bis * 100:.0f} % der Runde: {r.mittel_verlust:.2f} s" for r in top.itertuples()) +
                       ". Dort lohnt sich Überlagern der Runden im Telemetrie-Plot."))
    return out, segs


def lap_name(r) -> str:
    """Anzeige einer Runde: 'S2 Rd. 5' (Session + Runde in der Session)."""
    get = r.get if hasattr(r, "get") else lambda k, d=None: getattr(r, k, d)
    sid, num = get("session", None), get("num", None)
    if sid is None or num is None or sid != sid:
        return f"Runde {int(get('lap'))}"
    return f"S{int(sid) + 1} Rd. {int(num)}"


def _laptime(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:06.3f}"


def _fuel_laps(laps: pd.DataFrame) -> float | None:
    """Größte gefahrene Distanz einer ausgewerteten Session in Runden (inkl. Start/Out-/Inlap) – Basis für die Spritmenge."""
    if "dist" not in laps or laps.dist.isna().all():
        return None
    ref = laps[laps.clean].dist.median()
    if not ref or ref != ref:
        return None
    per = laps[laps.used].groupby("session").dist.sum() / ref
    return float(per.max()) if len(per) else None


def analyze_groups(df: pd.DataFrame, meta: dict | None = None, groups: dict | None = None) -> dict[str, Result]:
    """Eine Auswertung je Gruppe, z. B. {"race": [1, 3, 5], "quali": [0, 2, 4]} (Standard: default_groups)."""
    groups = groups if groups else default_groups(meta)
    return {m: analyze(df, meta, ids, "quali" if m == "quali" else "race")
            for m, ids in groups.items() if ids is None or ids}


# ----------------------------------------------------------------------------- Einstieg
def analyze(df: pd.DataFrame, meta: dict | None = None, sessions="auto", mode: str = "race") -> Result:
    """sessions: "auto" (Rennen/Longruns, sonst alle), None (alle) oder Liste von Session-IDs.
    mode: "race" (Stint-Logik, 1. Runde je Stint nicht im Druck-Ø) oder "quali" (alle gezeiteten Runden)."""
    meta = meta or {}
    tg = TARGET
    df = prepare(df)
    laps = lap_table(df, meta)
    if isinstance(sessions, str):
        sessions = default_sessions(meta)
    if sessions is not None:
        laps["used"] = laps.session.isin(list(sessions))
        laps["valid"] = laps.clean & laps.used
    clicks: dict = {}
    tyre_stats: dict = {}
    findings = []
    findings += analyze_tyres(df, laps, tg, clicks, meta.get("compound_last") or meta.get("compound"), tyre_stats, mode)
    findings += analyze_brakes(df, laps, tg, clicks)
    findings += analyze_balance(df, laps, tg)
    findings += analyze_traction(df, laps)
    findings += analyze_bumpstops(df, laps)
    findings += analyze_fuel(laps)
    drv, segs = analyze_driver(df, laps)
    findings += drv
    fast = df[(df.lap >= 0) & (df.speed > 80)].groupby("lap")
    lp = fast[[f"p_{w}" for w in WHEELS]].mean()
    lp["tc"] = fast[[f"tc_{w}" for w in WHEELS]].mean().mean(axis=1)
    stats = dict(stints=stint_table(df, laps, get_stints(laps)), sessions=session_table(df, laps, meta),
                 lap_p={int(k): v for k, v in lp.to_dict("index").items()},
                 selected=sorted(int(x) for x in laps[laps.used].session.unique()), mode=mode,
                 fuel_laps=_fuel_laps(laps),
                 samples=len(df), laps_total=len(laps), laps_valid=int(laps.valid.sum()),
                 lap_ref=float(laps[laps.valid].lap_s.median()) if laps.valid.any() else None,
                 **tyre_stats)
    if len(laps[laps.valid]) == 1:
        findings.append(Finding("Reifen", "info", "Nur 1 gültige Runde",
                                "Reifen sind in 1 Runde meist noch nicht im Betriebsfenster – Aussagen mit Vorsicht, besser 3+ Runden exportieren (Electronics → Telemetrie-Runden)."))
    return Result(laps, findings, segs, clicks, stats)
