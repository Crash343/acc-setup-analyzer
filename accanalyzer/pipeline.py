"""Ablauf hinter dem Fenster: auswerten (Rennen/Quali getrennt), Report schreiben, Setups speichern."""
from __future__ import annotations

import math
import statistics
from pathlib import Path

from . import analysis, report, setupio

SUFFIX = {"race": "rennen", "quali": "quali", "quali_kurz": "quali_kurz", "all": "optimiert"}


def _fuel_per_lap(sel: list[dict], meta: dict) -> float | None:
    """Gemessener Verbrauch je Runde aus dem Live-Mitschnitt (Sprit Start/Ende je Session, ohne Nachtanken)."""
    ref = meta.get("ref_lap_m")
    if not ref:
        return None
    vals = []
    for s in sel:
        lv = s.get("live") or {}
        a, b = lv.get("fuel_start"), lv.get("fuel_end")
        dist = sum(l["dist"] for l in meta.get("laps_info") or [] if l["session"] == s["id"]) / ref
        if a and b is not None and a > b and dist >= 2:
            vals.append((a - b) / dist)
    return round(statistics.median(vals), 3) if vals else None


def default_conditions(meta: dict, groups: dict | None = None, setup_path=None) -> dict:
    """Bedingungen je Auswertung: {mode: dict(air, road, minutes, quick, fuel_per_lap, src)}.

    Luft, Asphalt, Dauer und Verbrauch NUR aus dem Live-Mitschnitt (ACC Shared Memory) – ohne Mitschnitt bleiben sie
    leer (None) und können im Fenster bzw. per --air/--road/--race-min/--quali-min eingetragen werden.
    Keine Schätzungen: lieber keine Daten als falsche.
    """
    groups = groups if groups else analysis.default_groups(meta)
    ss = {s["id"]: s for s in meta.get("sessions") or []}
    out = {}
    for mode, ids in groups.items():
        sel = [ss[i] for i in (ids if ids is not None else list(ss)) if i in ss]
        live = [s["live"] for s in sel if s.get("live")]
        c: dict = dict(quick=setupio.QUICK_LAPS, air=None, road=None, minutes=None, src={})
        if live:
            c["air"] = round(statistics.median(x["air"] for x in live))
            c["road"] = round(statistics.median(x["road"] for x in live))
            c["src"].update(air="live", road="live")
            mins = [x["minutes"] for x in live if x.get("minutes")]
            if mins:
                c["minutes"], c["src"]["minutes"] = max(mins), "live"
        c["fuel_per_lap"] = _fuel_per_lap(sel, meta)
        out[mode] = c
    return out


def evaluate(df, meta: dict, groups: dict | None = None, setup: dict | None = None,
             cond: dict | None = None, setup_path=None) -> dict:
    """{mode: {"res": Result, "plan": Änderungsplan oder None, "cond": Bedingungen}} – eine Auswertung je Gruppe."""
    results = analysis.analyze_groups(df, meta, groups)
    base = default_conditions(meta, groups, setup_path)
    out = {}
    for m, r in results.items():
        c = dict(base.get(m) or {})
        over = {k: v for k, v in ((cond or {}).get(m) or {}).items() if v is not None and k != "src"}
        c.update(over)
        c["src"] = dict(c.get("src") or {}, **{k: "Eingabe" for k in over if k in ("air", "road", "minutes")})
        out[m] = dict(res=r, cond=c, plan=setupio.plan_for(setup, r, m, c, setup_path) if setup is not None else None)
    return out


def setup_name(cond: dict | None, fuel, fallback, key: str) -> str:
    """'20C_24C_40F.json' (Luft, Asphalt, Liter); ohne bekannte Temperaturen '<Original>_<variante>.json'."""
    c = cond or {}
    if c.get("air") is not None and c.get("road") is not None and fuel is not None:
        return f"{round(c['air'])}C_{round(c['road'])}C_{round(fuel)}F.json"
    return f"{Path(str(fallback)).stem}_{SUFFIX.get(key, key)}.json"


def variant_names(evals: dict, setup_path, prefix: str = "") -> dict[str, str]:
    """Dateinamen aller Export-Varianten, eindeutig (gleicher Name -> Variante anhängen; nie das Original)."""
    names, used = {}, {Path(str(setup_path)).name.lower()} if setup_path else set()
    for m, e in evals.items():
        for v in (e["plan"] or {}).get("variants") or []:
            n = prefix + setup_name(e["cond"], v["fuel"], setup_path or "setup", v["key"])
            if n.lower() in used:
                n = n[:-5] + f"_{SUFFIX.get(v['key'], v['key'])}.json"
            used.add(n.lower())
            names[v["key"]] = n
    return names


def variant_items(plan: dict, key: str, selected: list[dict] | None = None) -> list[dict]:
    """Änderungen einer Variante: gemeinsame Änderungen (oder die ausgewählten) + deren Sprit."""
    items = list(plan["items"] if selected is None else selected)
    v = next(v for v in plan["variants"] if v["key"] == key)
    return items + ([v["item"]] if v["item"] else [])


def write_report(df, meta: dict, evals: dict, out_dir: Path, stem: str, saved: dict | None = None,
                 names: dict | None = None) -> tuple[str, Path, Path | None]:
    """Schreibt <stem>_report.md/.html und <stem>_plots.png. Rückgabe: (markdown, html-Pfad, png-Pfad oder None)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {m: e["res"] for m, e in evals.items()}
    names = dict(names or {})
    names.update({k: Path(p).name for k, p in (saved or {}).items()})
    logs = None
    if any(e["plan"] is not None for e in evals.values()):
        logs = {m: setupio.plan_log(e["plan"], names) for m, e in evals.items()}
    md = report.to_markdown(results, meta, logs, {m: e.get("cond") for m, e in evals.items()})
    (out_dir / f"{stem}_report.md").write_text(md, encoding="utf-8")
    png = out_dir / f"{stem}_plots.png"
    has_png = report.make_plots(analysis.prepare(df), results, png)
    html = out_dir / f"{stem}_report.html"
    report.write_html(md, html, png if has_png else None)
    return md, html, png if has_png else None


def save_setups(setup: dict, evals: dict, out_dir: Path, setup_path, prefix: str = "") -> dict[str, Path]:
    """Ein Setup je Export-Variante (Rennen, Quali voll, Quali kurz), sofern sich etwas ändert. Rückgabe: {variante: Pfad}."""
    names = variant_names(evals, setup_path, prefix)
    saved = {}
    for m, e in evals.items():
        plan = e["plan"]
        if not plan:
            continue
        for v in plan["variants"]:
            items = variant_items(plan, v["key"])
            if not items:
                continue
            p = Path(out_dir) / names[v["key"]]
            setupio.save(setupio.apply_items(setup, items), str(p))
            saved[v["key"]] = p
    return saved
