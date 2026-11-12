"""Markdown-/HTML-Report und Diagramme.

Ein Report kann mehrere Auswertungen enthalten (z. B. Rennen und Quali einer Aufnahme), jede mit eigenen
Reifenwerten, Empfehlungen und eigenem Setup-Vorschlag.
"""
from __future__ import annotations

from pathlib import Path

from .analysis import MODES, PSI_PER_CLICK, Result, _laptime
from .schema import WHEELS

ICON = {"ok": "✅", "info": "ℹ️", "warn": "⚠️", "action": "🔧"}
ORDER = {"action": 0, "warn": 1, "info": 2, "ok": 3}
SESSION_COLORS = ["#2a6fdb", "#e08a00", "#2a9d4a", "#d33", "#8e44ad", "#16a2b8", "#7f6000", "#c2185b"]
WHEEL_DE = {"fl": "VL", "fr": "VR", "rl": "HL", "rr": "HR"}


def _lt(x: float) -> str:
    return "-" if x != x or x <= 0 else _laptime(x)


def _clock(s: float) -> str:
    """Zeitpunkt/Dauer in der Aufnahme als h:mm:ss / mm:ss."""
    s = int(round(s))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _num(x: float, fmt: str = ".0f") -> str:
    return "-" if x != x else format(x, fmt)


def session_label(s: dict) -> str:
    return f"S{s['id'] + 1} {s['type']}"


def mode_title(mode: str) -> str:
    return MODES.get(mode, mode)


def _as_dict(results) -> dict[str, Result]:
    return results if isinstance(results, dict) else {"all": results}


def session_modes(results) -> dict[int, str]:
    """Session-ID -> Auswertung, in die sie eingeht."""
    out = {}
    for mode, res in _as_dict(results).items():
        for sid in res.stats.get("selected", []):
            out[sid] = mode
    return out


# ----------------------------------------------------------------------------- Markdown
def cond_text(c: dict | None) -> str:
    """'Luft 20 °C · Asphalt 24 °C · Dauer 20 min' – nur Werte aus dem Live-Mitschnitt oder eigener Eingabe."""
    if not c:
        return ""
    src = c.get("src") or {}

    def tag(k):
        return " (Eingabe)" if src.get(k) == "Eingabe" else ""
    parts = [f"Luft {c['air']:.0f} °C{tag('air')}" if c.get("air") is not None else "Luft unbekannt",
             f"Asphalt {c['road']:.0f} °C{tag('road')}" if c.get("road") is not None else "Asphalt unbekannt",
             f"Dauer {c['minutes']} min{tag('minutes')}" if c.get("minutes") else "Dauer unbekannt"]
    if c.get("fuel_per_lap"):
        parts.append(f"Verbrauch {c['fuel_per_lap']:.2f} l/Runde")
    live = all(src.get(k) == "live" for k in ("air", "road"))
    return " · ".join(parts) + (" – live aus ACC" if live else "")


def session_temp(s: dict) -> str:
    lv = s.get("live")
    if lv:
        return f"{lv['air']:.0f} / {lv['road']:.0f} °C"
    return "–"   # ohne Live-Mitschnitt keine Temperaturen (keine Schätzung)


def to_markdown(results, meta: dict, setup_logs=None, conds: dict | None = None) -> str:
    """results: Result oder {mode: Result}; setup_logs: list[str] (eine Auswertung) oder {mode: list[str]};
    conds: {mode: Bedingungen} (Luft, Asphalt, Dauer)."""
    results = _as_dict(results)
    if setup_logs is not None and not isinstance(setup_logs, dict):
        setup_logs = {next(iter(results)): setup_logs}
    setup_logs = setup_logs or {}
    first = next(iter(results.values()))
    st = first.stats
    smode = session_modes(results)

    L = ["# ACC Telemetrie-Analyse",
         f"**{meta.get('car', '?')}** @ **{meta.get('track', '?')}** – Reifen: {meta.get('compound_last') or meta.get('compound', '?')}  "]
    parts = " · ".join(f"**{mode_title(m)}: {r.stats['laps_valid']} gültige Runden**" for m, r in results.items())
    L.append(f"{len(st.get('sessions') or [])} Session(s) · {st['laps_total']} Runden insgesamt · {parts}  ")
    for m in results:
        ct = cond_text((conds or {}).get(m))
        if ct:
            L.append(f"{mode_title(m)}: {ct}  ")
    if conds and not meta.get("live_sessions"):
        missing = any(c.get(k) is None for c in conds.values() if c for k in ("air", "road", "minutes"))
        L.append("_Kein Live-Mitschnitt zu dieser Aufnahme – fehlende Werte (Luft, Asphalt, Session-Dauer) werden nicht geschätzt. "
                 "Lass beim Fahren das Analyzer-Fenster offen, dann kommen sie direkt aus ACC, oder trag sie im Fenster selbst ein._"
                 if missing else "_Kein Live-Mitschnitt zu dieser Aufnahme – Luft, Asphalt und Dauer stammen aus deiner Eingabe._")
    L.append("")

    L += _session_overview(st, smode)
    if len(results) > 1:
        L.append("## Rennen und Quali im Vergleich")
        L += _compare_table(results)
    for mode, res in results.items():
        L += _mode_section(mode, res, setup_logs.get(mode), multi=len(results) > 1)

    stints = [s for r in results.values() for s in r.stats.get("stints") or []]
    if stints:
        sess = {s["id"]: s for s in st.get("sessions") or []}
        L.append("## Stints (ununterbrochene gültige Runden einer Session)")
        L.append("| Session | Runden | n | Median | Beste | Druck Beginn (VL/VR/HL/HR) | Druck Ende | Reifentemp. Ende | Trend |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for s in sorted(stints, key=lambda s: s["first"]):
            sl = "-" if s["slope"] != s["slope"] else f"{s['slope']:+.2f} s/Rd"
            name = session_label(sess[s["session"]]) if s.get("session") in sess else "-"
            L.append(f"| {name} | {s.get('num_first', s['first'])}–{s.get('num_last', s['last'])} | {s['n']} | {_lt(s['lap_median'])} | {_lt(s['lap_best'])} | "
                     f"{' / '.join(f'{x:.1f}' for x in s['p_early'])} | {' / '.join(f'{x:.1f}' for x in s['p_late'])} | {s['temp_late']:.0f} °C | {sl} |")
        L.append("\n_Nur Stints ab 3 Runden. Trend = Rundenzeit-Steigung über den Stint (bereinigt um Ausreißer); "
                 "enthält auch den Spritverbrauch (leichteres Auto), nicht nur Reifenabbau._\n")

    L += _lap_tables(first, smode)
    L.append("\n> Hinweis: Schwellwerte sind Heuristiken für ACC-GT3 (siehe `TARGET` in analysis.py). Immer nur 1–2 Änderungen gleichzeitig testen.")
    return "\n".join(L)


def _mode_section(mode: str, res: Result, setup_log, multi: bool) -> list[str]:
    st = res.stats
    sess = {s["id"]: s for s in st.get("sessions") or []}
    h = "##"
    L = []
    if multi:
        names = ", ".join(session_label(sess[i]) for i in st.get("selected", []) if i in sess)
        L.append(f"# {'🏁' if mode == 'race' else '⏱️'} {mode_title(mode)} – {names}")
        if mode == "quali":
            L.append("_Quali: alle gezeiteten Runden zählen (die Outlap wärmt die Reifen an). Wenige Runden → Werte mit Vorsicht._\n")
    L += _pressure_grid(st.get("pressure"), h)
    L += _temps_block(st.get("temps"), h)

    todo = [f for f in res.findings if f.level == "action"]
    L.append(f"{h} Empfehlungen{' ' + mode_title(mode) if multi else ''} (Reihenfolge nach Wichtigkeit)")
    if not todo:
        L.append("Keine eindeutigen Setup-Probleme gefunden. 👍")
    for i, f in enumerate(todo, 1):
        L.append(f"**{i}. {f.title}**  _(Sicherheit: {f.confidence})_  ")
        L.append(f"{f.detail}")
        for a in f.actions:
            L.append(f"- {a}")
        L.append("")
    if setup_log:
        L.append(f"{h} Setup-Datei{' ' + mode_title(mode) if multi else ''} (neue Datei, Original unverändert)")
        L += [f"- {x}" for x in setup_log]
        L.append("")
    L.append(f"{h} Alle Befunde{' ' + mode_title(mode) if multi else ''}")
    for f in sorted(res.findings, key=lambda f: ORDER[f.level]):
        L.append(f"- {ICON[f.level]} **[{f.area}] {f.title}** – {f.detail}")
    L.append("")
    return L


def _compare_table(results: dict[str, Result]) -> list[str]:
    modes = list(results)
    L = ["| | " + " | ".join(mode_title(m) for m in modes) + " |", "|---" * (len(modes) + 1) + "|"]

    def row(label, fn):
        L.append(f"| {label} | " + " | ".join(fn(results[m].stats) for m in modes) + " |")
    row("Gültige Runden", lambda s: str((s.get("pressure") or {}).get("n_laps", s["laps_valid"])))
    for w in WHEELS:
        row(f"Ø Druck {WHEEL_DE[w]} [psi]", lambda s, w=w: _num((s.get("pressure") or {}).get("avg", {}).get(w, float("nan")), ".2f"))
    for w in WHEELS:
        row(f"Ø Reifentemp. {WHEEL_DE[w]} [°C]", lambda s, w=w: _num((s.get("temps") or {}).get("tyre", {}).get(w, float("nan"))))
    row("Ø Bremse Spitze vorne / hinten [°C]", lambda s: _brake_axles(s.get("temps")))
    row("Druck-Empfehlung (Clicks VL/VR/HL/HR)", lambda s: " / ".join(
        f"{c:+d}" if c else "0" for c in ((s.get("pressure") or {}).get("clicks") or {w: 0 for w in WHEELS}).values()))
    L.append("")
    return L


def _brake_axles(t: dict | None) -> str:
    if not t:
        return "-"
    b = t["brake_peak"]
    return f"{_num((b['fl'] + b['fr']) / 2)} / {_num((b['rl'] + b['rr']) / 2)}"


def _temps_block(t: dict | None, h: str) -> list[str]:
    if not t:
        return []
    ty, br = t["tyre"], t["brake_peak"]
    return [f"{h} Temperaturen (Ø über dieselben Runden)",
            "| | Links | Rechts |", "|---|---|---|",
            f"| **Vorne** | VL · Reifen {_num(ty['fl'])} °C · Bremse {_num(br['fl'])} °C | VR · Reifen {_num(ty['fr'])} °C · Bremse {_num(br['fr'])} °C |",
            f"| **Hinten** | HL · Reifen {_num(ty['rl'])} °C · Bremse {_num(br['rl'])} °C | HR · Reifen {_num(ty['rr'])} °C · Bremse {_num(br['rr'])} °C |",
            "\n_Reifen = Ø Reifentemperatur bei > 80 km/h; Bremse = Ø der Spitzentemperatur je Runde._\n"]


def _session_overview(st: dict, smode: dict[int, str]) -> list[str]:
    ss = st.get("sessions") or []
    if len(ss) <= 1:
        return []
    L = ["## Sessions in dieser Aufnahme",
         "| Auswertung | Session | Beginn | Dauer | Luft / Asphalt | Runden (gültig) | Beste | Median | Ø Druck VL / VR / HL / HR | Ø Reifentemp. VL / VR / HL / HR |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for s in ss:
        p = " / ".join(_num(s["p"][w], ".2f") for w in WHEELS)
        t = " / ".join(_num(s["tc"][w]) for w in WHEELS)
        L.append(f"| {mode_title(smode[s['id']]) if s['id'] in smode else '–'} | **{session_label(s)}** | {_clock(s['start'])} | "
                 f"{_clock(s['dur'])} min | {session_temp(s)} | {s['laps']} ({s['laps_valid']}) | {_lt(s['best'])} | {_lt(s['median'])} | {p} | {t} °C |")
    L.append("\n_Sessions werden an Stillständen > 2 min getrennt; „Rennen“ = Start vom Grid oder Formationsrunde. "
             "Ø Druck/Temperatur: im Rennen ohne die 1. Runde jedes Stints, in der Quali alle gezeiteten Runden._\n")
    return L


def _lap_tables(res: Result, smode: dict[int, str]) -> list[str]:
    laps = res.laps
    ss = res.stats.get("sessions") or [dict(id=0, type="Session", dur=0)]
    L = ["## Runden"]
    lp = res.stats.get("lap_p", {})
    for s in ss:
        g = laps[laps.session == s["id"]]
        if g.empty:
            continue
        best = g[g.clean].lap_s.min() if g.clean.any() else float("nan")
        if len(ss) > 1:
            used = f" → Auswertung {mode_title(smode[s['id']])}" if s["id"] in smode else " (nicht ausgewertet)"
            L.append(f"### {session_label(s)} – {_clock(s['dur'])} min{used}")
        L.append("| Rd. | Typ | Zeit | Δ Beste | gültig | Druck VL / VR / HL / HR | Reifentemp. Ø |")
        L.append("|---|---|---|---|---|---|---|")
        for r in g.itertuples():
            delta = "" if not r.clean or best != best else ("**Bestzeit**" if r.lap_s == best else f"+{r.lap_s - best:.2f} s")
            p = lp.get(int(r.lap))
            ptxt = " / ".join(f"{p[f'p_{w}']:.1f}" for w in WHEELS) if p else "-"
            ttxt = f"{p['tc']:.0f} °C" if p and p["tc"] == p["tc"] else "-"
            zeit = _lt(r.lap_s)
            if r.clean and r.lap_s == best:
                zeit = f"**{zeit}**"
            L.append(f"| {r.num} | {r.kind} | {zeit} | {delta} | {'ja' if r.clean else 'nein'} | {ptxt} | {ttxt} |")
        L.append("")
    L.append("_Typ: Start = vom Grid bis zur Linie, Outlap = aus der Box, Box/Stopp = Runde mit Stillstand "
             "(Boxenstopp oder Return-to-Pit), Inlap = letzte Fahrt bis zum Session-Ende. Drücke = Mittel bei > 80 km/h._")
    return L


def _pressure_cell(name: str, avg: float, diff: float, clk: int) -> str:
    if clk == 0:
        how = "✅ passt"
    else:
        arrow = "⬇ runter" if clk < 0 else "⬆ rauf"
        how = f"{arrow} **{clk:+d} Click{'s' if abs(clk) != 1 else ''}** ({clk * PSI_PER_CLICK:+.1f} psi kalt)"
    return f"**{name}** · Ø {avg:.2f} psi · Δ {diff:+.2f} psi · {how}"


def wheel_style(clk: int) -> tuple[str, str, str]:
    """(Füllfarbe, Textfarbe, Kurztext) für ein Rad: zu hoch -> rot (runter), zu niedrig -> blau (rauf), passt -> grün."""
    if clk == 0:
        return "#3aa757", "#1e6b34", "passt"
    n = min(abs(clk), 5)
    if clk < 0:   # Druck zu hoch -> Clicks runter
        fill = ["#f2a796", "#ec8c77", "#e47259", "#d9553d", "#c8381f"][n - 1]
        return fill, "#a3301b", f"▼ {clk:+d} Click{'s' if abs(clk) != 1 else ''}"
    fill = ["#9cc0ee", "#82ade8", "#6597e0", "#3f82d8", "#1f64be"][n - 1]
    return fill, "#1b56a3", f"▲ {clk:+d} Click{'s' if abs(clk) != 1 else ''}"


def pressure_svg(p: dict) -> str:
    """Draufsicht Auto (vorne oben): jedes Rad eingefärbt nach nötiger Druckänderung, Werte daneben."""
    W, H = 560, 380
    cx = W / 2
    a, d, c = p["avg"], p["diff"], p["clicks"]
    pos = {"fl": (cx - 92, 70), "fr": (cx + 92, 70), "rl": (cx - 96, 250), "rr": (cx + 96, 250)}
    names = {"fl": "VL", "fr": "VR", "rl": "HL", "rr": "HR"}
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" style="max-width:100%;font-family:Segoe UI,system-ui,sans-serif">',
         f'<text x="{cx}" y="16" text-anchor="middle" font-size="12" fill="#666">▲ VORNE</text>',
         # Karosserie (Draufsicht)
         f'<path d="M{cx - 70},60 Q{cx - 74},30 {cx - 40},26 L{cx + 40},26 Q{cx + 74},30 {cx + 70},60 L{cx + 76},300 '
         f'Q{cx + 76},338 {cx + 40},342 L{cx - 40},342 Q{cx - 76},338 {cx - 76},300 Z" fill="#e9ecef" stroke="#6c757d" stroke-width="2"/>',
         f'<path d="M{cx - 44},128 Q{cx},112 {cx + 44},128 L{cx + 38},200 Q{cx},210 {cx - 38},200 Z" fill="#adb5bd"/>',   # Cockpit
         f'<rect x="{cx - 66}" y="326" width="132" height="12" rx="3" fill="#6c757d"/>']                                 # Heckflügel
    for w, (x, y) in pos.items():
        fill, tc, txt = wheel_style(c[w])
        s.append(f'<rect x="{x - 15}" y="{y - 30}" width="30" height="60" rx="7" fill="{fill}" stroke="#333" stroke-width="2"/>')
        right = x > cx
        tx = x + 30 if right else x - 30
        anc = "start" if right else "end"
        s.append(f'<text x="{tx}" y="{y - 14}" text-anchor="{anc}" font-size="15" font-weight="700" fill="#222">{names[w]}</text>')
        s.append(f'<text x="{tx}" y="{y + 6}" text-anchor="{anc}" font-size="17" font-weight="700" fill="{tc}">{txt}</text>')
        s.append(f'<text x="{tx}" y="{y + 25}" text-anchor="{anc}" font-size="12" fill="#555">Ø {a[w]:.2f} psi (Δ {d[w]:+.2f})</text>')
    s.append(f'<text x="{cx}" y="{H - 8}" text-anchor="middle" font-size="11" fill="#777">rot = Druck zu hoch → runter · blau = zu niedrig → rauf · '
             f'grün = passt · 1 Click = {PSI_PER_CLICK} psi</text></svg>')
    return "".join(s)


def _pressure_grid(p: dict | None, h: str = "##") -> list[str]:
    """Ø-Heißdruck je Rad als 2×2-Raster in Fahrzeuganordnung (vorne oben, links links)."""
    if not p or any(v != v for v in p["avg"].values()):   # keine Daten / NaN
        return []
    L = [f"{h} Reifendruck: Ø über {p['n_laps']} Runde(n) vs. Ziel {p['target']} psi heiß"]
    if p["wet"]:
        L.append("_Regenreifen – Druckziel gilt nur für Slicks, daher keine Anpassung._\n")
    a, d, c = p["avg"], p["diff"], p["clicks"]
    if not p["wet"]:
        L += ["", f'<div class="car">{pressure_svg(p)}</div>', ""]   # HTML-Block: Grafik im Browser-Report
    L.append("| | Links | Rechts |")
    L.append("|---|---|---|")
    L.append(f"| **Vorne** | {_pressure_cell('VL', a['fl'], d['fl'], c['fl'])} | {_pressure_cell('VR', a['fr'], d['fr'], c['fr'])} |")
    L.append(f"| **Hinten** | {_pressure_cell('HL', a['rl'], d['rl'], c['rl'])} | {_pressure_cell('HR', a['rr'], d['rr'], c['rr'])} |")
    first = "Die 1. Runde jedes Stints (Aufwärmen) ist nicht im Ø." if p.get("first_lap_excluded", True) else "Alle gezeiteten Runden im Ø."
    L.append(f"\n_Δ = Ø gemessen − {p['target']} psi; bis ±0,1 psi gilt als „passt“. Anpassung im Setup unter Reifen → Reifendruck (kalt), "
             f"1 Click = {PSI_PER_CLICK} psi ≈ gleiche Änderung heiß. {first}_\n")
    return L


# ----------------------------------------------------------------------------- Diagramme
def make_plots(df, results, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    results = _as_dict(results)
    smode = session_modes(results)
    first = next(iter(results.values()))
    laps = first.laps
    used = laps.session.isin(list(smode)) & laps.clean
    v = laps[used] if used.any() else laps[laps.clean]
    if v.empty:
        return False
    best = v.loc[v.lap_s.idxmin(), "lap"]
    ss = {s["id"]: s for s in first.stats.get("sessions") or []}
    style = {"race": "-", "quali": "--", "all": "-"}

    def name(sid):
        s = ss.get(sid, dict(id=sid, type="Session"))
        return session_label(s) + ("" if sid in smode else " (nicht ausgewertet)")

    fig = plt.figure(figsize=(13, 12))
    gs = fig.add_gridspec(3, 2)

    # 1) Rundenzeiten je Session (nur gültige Runden), x = Rundennummer in der Session
    a = fig.add_subplot(gs[0, :])
    for sid, g in laps[laps.clean].groupby("session"):
        col = SESSION_COLORS[sid % len(SESSION_COLORS)]
        a.plot(g.num, g.lap_s, marker="o", color=col, ls=style.get(smode.get(sid), ":"),
               alpha=1 if sid in smode else .5, label=name(sid))
    ylo, yhi = laps[laps.clean].lap_s.min(), laps[laps.clean].lap_s.max()
    a.set_ylim(ylo - 1, yhi + 1)
    a.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: _laptime(x)))
    a.set_title("Rundenzeiten je Session (gültige Runden; durchgezogen = Rennen, gestrichelt = Quali)")
    a.set_xlabel("Runde in der Session")
    a.grid(alpha=.3)
    a.legend(fontsize=8, ncol=3)

    # 2) Beste Runde: Speed, Gas, Bremse
    a = fig.add_subplot(gs[1, 0])
    g = df[df.lap == best]
    a.plot(g.pos, g.speed, color="#2a6fdb", label="km/h")
    r = laps.set_index("lap").loc[best]
    a.set_title(f"Beste Runde ({session_label(ss[r.session]) if r.session in ss else ''} Rd. {r.num}, {_laptime(r.lap_s)})")
    a.set_xlabel("Rundenposition")
    b = a.twinx()
    b.fill_between(g.pos, 0, g.brake, color="#d33", alpha=.35, label="Bremse")
    b.plot(g.pos, g.gas, color="#2a9d4a", lw=.8, label="Gas")
    b.set_ylim(0, 1.6)

    # 3-5) Druck, Reifentemp., Bremstemp. je Runde; Linien je Session getrennt, x = fortlaufende Rundennummer
    cols = {"fl": "#2a6fdb", "fr": "#e08a00", "rl": "#2a9d4a", "rr": "#d33"}
    use = laps[laps.kind.isin(["gezeitet", "Box/Stopp"]) | laps.clean]
    d = df[df.lap.isin(use.lap)]
    per_lap_mean = d[d.speed > 80].groupby("lap")
    per_lap = d.groupby("lap")
    panels = ((fig.add_subplot(gs[1, 1]), "p", "Reifendruck [psi] je Runde", per_lap_mean, "mean"),
              (fig.add_subplot(gs[2, 0]), "tc", "Reifentemperatur [°C] je Runde", per_lap_mean, "mean"),
              (fig.add_subplot(gs[2, 1]), "bt", "Bremsentemp. Spitze [°C] je Runde", per_lap, "max"))
    for axx, key, ttl, grp, how in panels:
        for w in WHEELS:
            ser = getattr(grp[f"{key}_{w}"], how)()
            for sid, gl in use.groupby("session"):
                y = ser.reindex(gl.lap)
                axx.plot(gl.lap, y.values, marker=".", color=cols[w], label=w.upper() if sid == use.session.min() else None)
        for sid, gl in use.groupby("session"):
            if sid not in ss:
                continue
            axx.axvspan(gl.lap.min() - .5, gl.lap.max() + .5, color=SESSION_COLORS[sid % len(SESSION_COLORS)],
                        alpha=.08 if sid in smode else .02)
            tag = {"race": "R", "quali": "Q"}.get(smode.get(sid), "")
            axx.text(gl.lap.mean(), 1.0, f"S{sid + 1}{' ' + tag if tag else ''}", transform=axx.get_xaxis_transform(),
                     ha="center", va="bottom", fontsize=8)
        if key == "p" and first.stats.get("pressure"):
            axx.axhline(first.stats["pressure"]["target"], color="#555", ls="--", lw=1, label="Ziel")
        axx.set_title(ttl, pad=14)
        axx.set_xlabel("Runde (fortlaufend, Bänder = Sessions; R = Rennen, Q = Quali)")
        axx.legend(fontsize=8)
        axx.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return True


def write_html(md: str, path: Path, png: Path | None = None):
    """Markdown-Report als eigenständige HTML-Datei (Diagramm eingebettet), im Browser lesbar."""
    import base64
    try:
        import markdown
        body = markdown.markdown(md, extensions=["tables"])
    except ImportError:
        import html
        body = "<pre>" + html.escape(md) + "</pre>"
    img = ""
    if png and Path(png).exists():
        img = '<h2>Diagramme</h2><img style="max-width:100%" src="data:image/png;base64,' + base64.b64encode(Path(png).read_bytes()).decode() + '">'
    css = ("body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:1150px;margin:2rem auto;padding:0 1rem;color:#222}"
           "table{border-collapse:collapse;margin-bottom:1rem}td,th{border:1px solid #ccc;padding:4px 8px}th{background:#f3f3f3}"
           "h1{border-bottom:2px solid #444;margin-top:2.5rem}h2{margin-top:2rem}h3{margin-top:1.5rem}code{background:#f3f3f3;padding:1px 4px}")
    Path(path).write_text(f"<!doctype html><meta charset='utf-8'><title>ACC Analyse</title><style>{css}</style>{body}{img}", encoding="utf-8")
