"""Einfaches Fenster: MoTeC-Aufnahme + (optional) Setup wählen, Sessions Rennen/Quali zuordnen, Empfehlungen ansehen,
je ein optimiertes Setup für Rennen und Quali speichern.

Start:  python -m accanalyzer   (oder Doppelklick auf die .exe)
"""
from __future__ import annotations

import gc
import os
import queue
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import analysis, compare, livebalance, livelog, livesession, pipeline, report, setupio
from . import knowledge as K
from .livelog import docs_dir
from .schema import WHEELS

ICON = {"ok": "✔", "info": "ℹ", "warn": "⚠", "action": "🔧"}
CHOICES = {"Rennen": "race", "Quali": "quali", "–": None}
TAB_TITLE = {"race": "🏁 Rennen", "quali": "⏱ Quali", "all": "Auswertung"}


def acc_dir() -> Path:
    return docs_dir() / "Assetto Corsa Competizione"


def newest_ld(folder: Path) -> Path | None:
    try:
        files = sorted(folder.glob("*.ld"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return files[0] if files else None


def setup_folder(meta: dict | None) -> Path:
    """Setups/<Auto>/<Strecke> passend zur Aufnahme, sonst so weit wie vorhanden."""
    base = acc_dir() / "Setups"
    if not meta:
        return base
    car = base / str(meta.get("car", ""))
    if not car.is_dir():
        return base
    track = str(meta.get("track", "")).lower()
    try:
        for d in car.iterdir():
            if d.is_dir() and track and (d.name.lower() == track or d.name.lower().startswith(track) or track.startswith(d.name.lower())):
                return d
    except OSError:
        pass
    return car


class ModeTab(ttk.Frame):
    """Ein Reiter je Auswertung (Rennen/Quali): Empfehlungen oben, Setup-Änderungen + Speichern unten."""

    NO_SETUP = ("Kein Setup gewählt. Wähle oben unter „Gefahrenes Setup“ die .json-Datei, mit der du gefahren bist "
                "(Dokumente\\Assetto Corsa Competizione\\Setups\\<Auto>\\<Strecke>), dann erscheinen hier die "
                "konkreten Änderungen und du kannst ein eigenes Setup für diese Auswertung speichern.")

    def __init__(self, master, app: "App", mode: str):
        super().__init__(master)
        self.app, self.mode = app, mode
        self.evals = self.setup = None
        self.change_vars: list[tuple[tk.BooleanVar, dict]] = []
        self._header()
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True)
        self.txt = _text(pw)
        pw.add(self.txt, weight=3)
        self.setup_box = ttk.LabelFrame(pw, text=f"Setup-Datei {analysis.MODES.get(mode, '')}")
        pw.add(self.setup_box, weight=2)

    def _header(self):
        pass

    def fill(self, res, plan, cond=None, evals=None, setup=None):
        self.cond = cond
        self.evals = evals if evals is not None else self.app.evals
        self.setup = setup if setup is not None else self.app.setup
        self._fill_text(res)
        self._fill_setup(plan)

    def _fill_text(self, res):
        t, st = self.txt, res.stats
        t.configure(state="normal")
        t.delete("1.0", "end")
        sess = {s["id"]: s for s in st.get("sessions") or []}
        used = ", ".join(report.session_label(sess[i]) for i in st.get("selected", []) if i in sess) or "alle"
        t.insert("end", f"{analysis.MODES.get(self.mode, '')}: {used}\n", "h")
        ct = report.cond_text(getattr(self, "cond", None))
        if ct:
            t.insert("end", ct + "\n", "dim")
        t.insert("end", f"{st['laps_valid']} gültige Runden.", "dim")
        if self.mode == "quali":
            t.insert("end", " Alle gezeiteten Runden zählen (die Outlap wärmt die Reifen an). Wenige Runden → Werte mit Vorsicht.", "dim")
        t.insert("end", "\n")

        p = st.get("pressure")
        names = dict(zip(WHEELS, K.WHEEL_DE))
        if p and all(v == v for v in p["avg"].values()):
            t.insert("end", f"Reifendruck heiß (Ziel {p['target']} psi, Ø über {p['n_laps']} Runden)\n", "h")
            if p.get("excluded_sessions"):
                t.insert("end", "Nicht im Ø (weicht stark ab): " + ", ".join(f"S{x + 1}" for x in p["excluded_sessions"]) + "\n", "warn")
            if not p["wet"]:
                t.window_create("end", window=car_canvas(t, p), padx=6, pady=4)
                t.insert("end", "\n")
            for row in (("fl", "fr"), ("rl", "rr")):
                for w in row:
                    c = p["clicks"][w]
                    how = "passt" if c == 0 else f"{c:+d} Click{'s' if abs(c) != 1 else ''} ({c * analysis.PSI_PER_CLICK:+.1f} psi kalt)"
                    t.insert("end", f"  {names[w]}: Ø {p['avg'][w]:.2f} psi (Δ {p['diff'][w]:+.2f}) → ")
                    t.insert("end", how, "b" if c else "")
                    t.insert("end", "      ")
                t.insert("end", "\n")
        tp = st.get("temps")
        if tp:
            t.insert("end", "Temperaturen (Ø über dieselben Runden)\n", "h")
            for row in (("fl", "fr"), ("rl", "rr")):
                t.insert("end", "  " + "      ".join(
                    f"{names[w]}: Reifen {report._num(tp['tyre'][w])} °C, Bremse {report._num(tp['brake_peak'][w])} °C" for w in row) + "\n")
        corner_lines(t, st.get("corners"))

        todo = [f for f in res.findings if f.level == "action"]
        t.insert("end", "Empfehlungen\n", "h")
        if not todo:
            t.insert("end", "Keine eindeutigen Setup-Probleme gefunden.\n")
        for i, f in enumerate(todo, 1):
            t.insert("end", f"{i}. {f.title}", "b")
            t.insert("end", f"  (Sicherheit: {f.confidence})\n", "dim")
            t.insert("end", f"{f.detail}\n")
            for a in f.actions:
                t.insert("end", f"   • {a}\n")
            t.insert("end", "\n")
        t.insert("end", "Weitere Befunde\n", "h")
        for f in sorted((f for f in res.findings if f.level != "action"), key=lambda f: report.ORDER[f.level]):
            t.insert("end", f"{ICON[f.level]} [{f.area}] {f.title}", "b")
            t.insert("end", f" – {f.detail}\n")
            for a in f.actions:
                t.insert("end", f"   • {a}\n", "dim")
        t.configure(state="disabled")

    def _fill_setup(self, plan):
        box = self.setup_box
        for w in box.winfo_children():
            w.destroy()
        self.change_vars = []
        if plan is None:
            ttk.Label(box, text=self.NO_SETUP, wraplength=950, justify="left").pack(anchor="w", padx=10, pady=8)
            return
        self.plan = plan
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True, padx=8, pady=4)
        if not plan["items"]:
            ttk.Label(inner, text="Keine Änderungen an Druck/Fahrwerk nötig – die Daten zeigen kein eindeutiges Problem.").pack(anchor="w")
        for it in plan["items"]:
            v = tk.BooleanVar(value=True)
            self.change_vars.append((v, it))
            ttk.Checkbutton(inner, text=it["text"], variable=v).pack(anchor="w")
        for n in plan["notes"]:
            ttk.Label(inner, text=n, foreground="#555", wraplength=950, justify="left").pack(anchor="w", pady=(2, 0))
        if plan["applied"]:
            ttk.Label(inner, text="Enthaltene Pakete: " + ", ".join(f"{f.title} → {K.FIXES[f.fix]['name']}" for f in plan["applied"]),
                      wraplength=950, foreground="#444").pack(anchor="w")
        for f in plan["skipped"]:
            ttk.Label(inner, text=f"Nicht übernommen (erst die ersten Änderungen testen): {f.title}", foreground="#777").pack(anchor="w")
        ttk.Label(inner, text="Export – je Variante eine Datei, Name = Luft °C _ Asphalt °C _ Liter:",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 2))
        names = pipeline.variant_names(self.evals, self.app.setup_var.get().strip().strip('"'))
        for v in plan["variants"]:
            row = ttk.Frame(inner)
            row.pack(anchor="w", fill="x", pady=2)
            ttk.Button(row, text=f"💾 {v['label']} speichern …", width=30,
                       command=lambda k=v["key"], n=names.get(v["key"]): self.save(k, n)).pack(side="left")
            ttk.Label(row, text=f"  {names.get(v['key'], '')}  –  {v['txt']}", foreground="#444",
                      wraplength=760, justify="left").pack(side="left")
        ttk.Label(inner, text="Standardordner: neben deinem Setup (ACC zeigt die Datei dann direkt an). Original bleibt unverändert.",
                  foreground="#777").pack(anchor="w", pady=(4, 0))

    def save(self, key: str, name: str | None):
        sel = pipeline.variant_items(self.plan, key, [it for v, it in self.change_vars if v.get()])
        cond = getattr(self, "cond", None) or {}
        if (cond.get("air") is None or cond.get("road") is None) and messagebox.askyesno(
                "Temperaturen unbekannt", "Luft/Asphalt sind unbekannt (kein Live-Mitschnitt zu dieser Aufnahme).\n"
                                          "Ohne Werte bekommt die Datei keinen Namen im Format 20C_24C_40F.\n\n"
                                          "Erst oben unter „Bedingungen“ eintragen und „Übernehmen“ klicken?"):
            return
        src = Path(self.app.setup_var.get().strip().strip('"'))
        p = filedialog.asksaveasfilename(title="Setup speichern", initialdir=str(src.parent),
                                         initialfile=name or pipeline.setup_name(None, None, src, key), defaultextension=".json",
                                         filetypes=[("ACC Setup", "*.json")])
        if not p:
            return
        if Path(p).resolve() == src.resolve() and not messagebox.askyesno(
                "Original überschreiben?", "Das ist dein Original-Setup. Wirklich überschreiben?"):
            return
        try:
            setupio.save(setupio.apply_items(self.setup, sel), p)
        except Exception as e:
            messagebox.showerror("Speichern fehlgeschlagen", str(e))
            return
        self.app.status.set(f"Gespeichert: {p}")
        messagebox.showinfo("Gespeichert", f"Setup gespeichert:\n{p}\n\n"
                                           "In ACC unter Setup → Laden auswählen (ggf. Setup-Liste neu öffnen).")


def corner_lines(t: ScrolledText, corners: list | None):
    """Balance je Kurve als Textblock (blau = Untersteuern, rot = Übersteuern)."""
    if not corners:
        return
    t.insert("end", "Balance je Kurve\n", "h")
    t.insert("end", f"  {'Kurve':6s}{'Pos.':>6s}{'min.':>10s}   " + "".join(f"{x:24s}" for x in analysis.PHASE_DE.values()) + "\n", "monob")
    for c in corners:
        t.insert("end", f"  {'K' + str(c['no']):6s}{c['apex'] * 100:5.0f}%{c['vmin']:5.0f} km/h   ", "mono")
        for k in analysis.PHASE_DE:
            p = c["phases"].get(k)
            bad = p is not None and p["verdict"] != "neutral"
            t.insert("end", f"{analysis.verdict_text(p):24s}", ("mono", p["verdict"]) if bad else "mono")
        t.insert("end", "\n")
    t.insert("end", "blau = Untersteuern (schiebt über die Vorderräder), rot = Übersteuern (Heck kommt). Pos. = % der Runde ab "
                    "Start/Ziel, min. = langsamste Stelle. K1, K2 … sind eigene Nummern, nicht die offiziellen Kurven. "
                    "Einzelne Kurven sind oft Fahrstil – Setup erst ändern, wenn es sich über mehrere Kurven zeigt.\n", "dim")


class LiveTab(ModeTab):
    """Auswertung direkt aus ACC (Shared Memory), ohne MoTeC-File – z. B. mit dem Aggressive-Preset auf neuer Strecke."""

    NO_SETUP = ("Kein Setup gewählt – die Empfehlungen oben gelten trotzdem (in Clicks relativ zu dem, was du fährst).\n"
                "Zum Speichern braucht das Tool die Setup-Datei, mit der du fährst. Bei einem ACC-Preset (z. B. Aggressive): in ACC im "
                "Setup-Menü unter eigenem Namen speichern, dann hier unten „Neuestes Setup verwenden“ oder „Setup-Datei wählen“. "
                "Danach erscheinen die Änderungen und die Speichern-Knöpfe.")
    MODE_CHOICES = {"automatisch": None, "Rennen": "race", "Quali": "quali"}

    def __init__(self, master, app: "App"):
        super().__init__(master, app, "race")

    def _header(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=(4, 2))
        self.info = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="automatisch")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Button(bar, text="Jetzt auswerten", command=lambda: self.app.run_live(manual=True)).pack(side="left")
        ttk.Checkbutton(bar, text="nach jeder Runde automatisch", variable=self.auto_var).pack(side="left", padx=10)
        ttk.Label(bar, text="Auswerten als:").pack(side="left")
        ttk.Combobox(bar, textvariable=self.mode_var, values=list(self.MODE_CHOICES), width=12, state="readonly").pack(side="left", padx=4)
        self.btn_report = ttk.Button(bar, text="Report öffnen", state="disabled", command=self.app.open_live_report)
        self.btn_report.pack(side="right")
        ttk.Label(self, textvariable=self.info, foreground="#0a6", wraplength=1050, justify="left").pack(anchor="w", padx=8)
        self._balance_box()

    def _balance_box(self):
        """Echtzeit Unter-/Übersteuern mit Ton (wie bei ACC Race Engineer)."""
        box = ttk.LabelFrame(self, text="Unter-/Übersteuern in Echtzeit – Zähler je Bremsen / Rollen / Gas")
        box.pack(fill="x", padx=4, pady=(2, 4))
        st = livebalance.load_settings()
        self.bal_vars = {}
        for r, (kind, name) in enumerate((("us", "Untersteuern"), ("os", "Übersteuern"))):
            for c, (spd, sname) in enumerate((("slow", "langsam"), ("fast", "schnell"))):
                ttk.Label(box, text=f"{sname} {'US' if kind == 'us' else 'OS'}:").grid(row=r, column=2 * c, sticky="e", padx=(8, 2))
                v = tk.StringVar(value="0 / 0 / 0")
                self.bal_vars[(spd, kind)] = v
                ttk.Label(box, textvariable=v, width=12, foreground="#1c5fd1" if kind == "us" else "#c62828",
                          font=("Consolas", 10, "bold")).grid(row=r, column=2 * c + 1, sticky="w")
        self.bal_now = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.bal_now, width=26, font=("Segoe UI", 10, "bold")).grid(row=0, column=4, rowspan=2, padx=10)
        self.set_vars = {}
        col = 5
        for key, lab, w in (("us_deg", "US-Schwelle °", 5), ("os_deg", "OS-Schwelle °", 5), ("slow_kmh", "langsam unter km/h", 5)):
            ttk.Label(box, text=lab).grid(row=0, column=col, sticky="e", padx=(8, 2))
            v = tk.StringVar(value=f"{st[key]:g}")
            v.trace_add("write", lambda *_: self.app.apply_balance_settings())
            self.set_vars[key] = v
            ttk.Entry(box, textvariable=v, width=w).grid(row=0, column=col + 1, sticky="w")
            col += 2
        self.sound_var = tk.BooleanVar(value=bool(st["sound"]))
        ttk.Checkbutton(box, text="Ton", variable=self.sound_var, command=self.app.apply_balance_settings).grid(row=1, column=5, sticky="w", padx=8)
        btns = ttk.Frame(box)
        btns.grid(row=1, column=6, columnspan=5, sticky="w")
        ttk.Button(btns, text="Ton US testen", command=lambda: self.app.test_beep("us")).pack(side="left", padx=2)
        ttk.Button(btns, text="Ton OS testen", command=lambda: self.app.test_beep("os")).pack(side="left", padx=2)
        ttk.Button(btns, text="Zähler zurücksetzen", command=self.app.reset_balance).pack(side="left", padx=2)
        ttk.Label(box, text="Tief = Untersteuern, hoch = Übersteuern; je stärker, desto schneller die Töne. Schwelle = Schräglauf vorne − hinten in Grad "
                            "(kleiner = empfindlicher). Zähler: Bremsen / Rollen / Gas, langsam/schnell nach der km/h-Grenze.",
                  foreground="#666", wraplength=1050).grid(row=2, column=0, columnspan=12, sticky="w", padx=8, pady=(2, 0))

    def balance_settings(self) -> dict:
        out = dict(sound=bool(self.sound_var.get()))
        for key, v in self.set_vars.items():
            try:
                out[key] = float(v.get().replace(",", "."))
            except ValueError:
                pass
        return out

    def _fill_setup(self, plan):
        if plan is not None:
            return super()._fill_setup(plan)
        box = self.setup_box
        for w in box.winfo_children():
            w.destroy()
        self.change_vars = []
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True, padx=10, pady=8)
        ttk.Label(inner, text=self.NO_SETUP, wraplength=950, justify="left").pack(anchor="w")
        row = ttk.Frame(inner)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="📂 Setup-Datei wählen …", command=self.app.pick_setup).pack(side="left")
        newest = newest_setup(self.app.live_meta)
        if newest is not None:
            when = time.strftime("%d.%m. %H:%M", time.localtime(newest.stat().st_mtime))
            ttk.Button(row, text=f"Neuestes Setup verwenden: {newest.name} ({when})",
                       command=lambda: self.app.use_setup(str(newest))).pack(side="left", padx=8)

    def show_intro(self):
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "Live-Auswertung – ohne MoTeC-File\n", "h")
        t.insert("end", "Lass dieses Fenster offen, während du in ACC fährst (unten „Live-Mitschnitt“ an). Ab der 2. abgeschlossenen "
                        "Runde erscheinen hier nach jeder Runde Reifendrücke, Temperaturen, Unter-/Übersteuern je Kurve und die "
                        "Setup-Vorschläge – schon während der Session.\n\n")
        t.insert("end", "Neue Strecke, noch kein Setup?\n", "b")
        t.insert("end", "1. In ACC das Preset „Aggressive“ laden und 3–5 saubere Runden am Stück fahren.\n"
                        "2. Hier die Empfehlungen ansehen (die 1. Runde nach der Box zählt beim Druck nicht – Reifen noch kalt).\n"
                        "3. Das Preset in ACC im Setup-Menü unter eigenem Namen speichern und oben als „Gefahrenes Setup“ wählen – "
                        "dann unten das angepasste Setup speichern, in ACC laden und weiterfahren.\n\n")
        t.insert("end", "Live misst ACC zusätzlich den Schräglauf jedes Rads – damit ist Unter-/Übersteuern genauer als aus dem "
                        "MoTeC-File. Die Daten der laufenden Session bleiben nur im Speicher; bei einer neuen Session beginnt die "
                        "Live-Auswertung von vorn.\n", "dim")
        t.configure(state="disabled")
        self._fill_setup(None)


WORST_COLORS = ("#d50000", "#ff6d00", "#ffb300")   # 1./2./3. größter Zeitverlust (Karte, Text)


def _lap_label(l: dict) -> str:
    return f"S{l['session'] + 1} Rd. {l['num']}  {report._lt(l['dur'])}"


class CompareTab(ttk.Frame):
    """Deine Runde gegen eine Referenzrunde (z. B. Hotlap eines Esport-Fahrers): wo verlierst du Zeit und warum."""

    def __init__(self, master, app: "App"):
        super().__init__(master)
        self.app = app
        self.ref_df = self.ref_meta = self.ref_path = None
        self.result = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(bar, text="Referenz:").grid(row=0, column=0, sticky="e")
        self.ref_var = tk.StringVar()
        self.ref_box = ttk.Combobox(bar, textvariable=self.ref_var, width=70, postcommand=self.refresh_refs)
        self.ref_box.grid(row=0, column=1, columnspan=3, sticky="ew", padx=4)
        self.ref_box.bind("<<ComboboxSelected>>", lambda e: self.load_ref())
        ttk.Button(bar, text="Durchsuchen …", command=self.pick_ref).grid(row=0, column=4, padx=2)
        ttk.Button(bar, text="Referenz-Ordner öffnen", command=self.open_folder).grid(row=0, column=5, padx=2)
        ttk.Label(bar, text="Deine Runde:").grid(row=1, column=0, sticky="e")
        self.own_var = tk.StringVar()
        self.own_box = ttk.Combobox(bar, textvariable=self.own_var, width=26, state="readonly")
        self.own_box.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(bar, text="Referenz-Runde:").grid(row=1, column=2, sticky="e")
        self.refl_var = tk.StringVar()
        self.refl_box = ttk.Combobox(bar, textvariable=self.refl_var, width=26, state="readonly")
        self.refl_box.grid(row=1, column=3, sticky="w", padx=4)
        ttk.Button(bar, text="Vergleichen", command=self.run).grid(row=1, column=4, padx=2, sticky="w")
        ttk.Button(bar, text="🗺 Karte groß", command=self.open_big_map).grid(row=1, column=5, padx=2, sticky="w")
        self.big_win = self.big_fig = self.big_canvas = self.big_ax = None
        bar.columnconfigure(1, weight=1)

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True)
        upper = ttk.PanedWindow(pw, orient="horizontal")
        pw.add(upper, weight=2)
        self.txt = _text(upper)
        upper.add(self.txt, weight=2)
        tf = ttk.Frame(upper)
        upper.add(tf, weight=3)
        cols = ("k", "pos", "dt", "brake", "vmin", "gas", "why")
        heads = ("Kurve", "Pos.", "Δ Zeit", "Bremspunkt", "Kurvenminimum", "Vollgas", "Unterschied")
        widths = (50, 45, 65, 110, 110, 100, 380)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=8)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "why" else "center", stretch=c == "why")
        self.tree.tag_configure("lost", foreground="#c62828")
        self.tree.tag_configure("won", foreground="#2e7d32")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._zoom_section)
        self.plot_frame = ttk.Frame(pw)
        pw.add(self.plot_frame, weight=3)
        self.canvas = self.fig = None
        self.intro()

    # ------------------------------------------------------------------ Anzeige
    def intro(self):
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "Vergleich mit einer Referenzrunde\n", "h")
        t.insert("end", "1. Referenz-MoTeC (.ld + .ldx, z. B. Hotlap eines Esport-Fahrers) für dasselbe Auto und dieselbe Strecke "
                        "in den Referenz-Ordner legen (Knopf oben). Unterordner sind erlaubt. Ein Setup (.json) daneben wird mitverglichen.\n"
                        "2. Oben im Reiter „Dateien“ deine eigene Aufnahme laden.\n"
                        "3. Referenz wählen → „Vergleichen“. Standard: jeweils die schnellste gültige Runde.\n\n", "")
        t.insert("end", f"Referenz-Ordner: {compare.ref_dir()}\n", "dim")
        t.configure(state="disabled")

    def refresh_refs(self):
        refs = compare.find_refs(self.app.meta)
        base = compare.ref_dir()
        self._refs = {}
        for r in refs:
            try:
                label = str(r.relative_to(base))
            except ValueError:
                label = str(r)
            self._refs[label] = r
        self.ref_box["values"] = list(self._refs)

    def own_loaded(self):
        """Nach dem Laden einer eigenen Aufnahme: Rundenliste füllen."""
        laps = compare.valid_laps(self.app.meta or {})
        self._own = {_lap_label(l): l for l in laps}
        self.own_box["values"] = list(self._own)
        if self.own_var.get() not in self._own:
            self.own_var.set(next(iter(self._own), ""))
        self.refresh_refs()
        if self.ref_df is not None:
            self.run()

    def pick_ref(self):
        base = compare.ref_dir()
        p = filedialog.askopenfilename(title="Referenz-MoTeC wählen", initialdir=str(base) if base.is_dir() else None,
                                       filetypes=[("ACC MoTeC", "*.ld"), ("Alle Dateien", "*.*")])
        if p:
            self.ref_var.set(p)
            self.load_ref()

    def open_folder(self):
        d = compare.ref_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except (AttributeError, OSError):
            messagebox.showinfo("Referenz-Ordner", str(d))

    def load_ref(self):
        v = self.ref_var.get().strip().strip('"')
        p = getattr(self, "_refs", {}).get(v) or Path(v)
        if not p.is_file():
            messagebox.showwarning("Referenz", "Bitte eine .ld-Datei wählen.")
            return

        def fn():
            from .ldimport import load_ld
            return p, *load_ld(p)
        self.app._work(fn, self._ref_loaded, f"Lade Referenz {p.name} …")

    def _ref_loaded(self, val):
        self.ref_path, self.ref_df, self.ref_meta = val
        laps = compare.valid_laps(self.ref_meta)
        self._ref_laps = {_lap_label(l): l for l in laps}
        self.refl_box["values"] = list(self._ref_laps)
        self.refl_var.set(next(iter(self._ref_laps), ""))
        self.app.status.set(f"Referenz geladen: {self.ref_path.name} ({len(laps)} gültige Runden)")
        if not laps:
            messagebox.showwarning("Referenz", "In der Referenz gibt es keine gültige gezeitete Runde.")
            return
        if self.app.df is not None:
            self.run()

    def run(self):
        if self.app.df is None:
            messagebox.showinfo("Vergleich", "Erst oben deine eigene Aufnahme laden.")
            return
        if self.ref_df is None:
            messagebox.showinfo("Vergleich", "Erst eine Referenz wählen.")
            return
        own = getattr(self, "_own", {}).get(self.own_var.get())
        ref = getattr(self, "_ref_laps", {}).get(self.refl_var.get())
        try:
            self.result = compare.compare(self.app.df, self.app.meta, self.ref_df, self.ref_meta, own, ref)
        except Exception as e:
            messagebox.showerror("Vergleich", str(e))
            return
        self._show()

    def _show(self):
        c, t = self.result, self.txt
        own_m, ref_m = self.app.meta, self.ref_meta
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "Wo verlierst du Zeit?\n", "h")
        t.insert("end", f"Du: {_lap_label(c['own_lap'])}  ·  Referenz: {_lap_label(c['ref_lap'])} ({self.ref_path.name})\n", "dim")
        if str(own_m.get("car")) != str(ref_m.get("car")) or str(own_m.get("track")).lower() != str(ref_m.get("track")).lower():
            t.insert("end", f"⚠ Andere(s) Auto/Strecke: du {own_m.get('car')} @ {own_m.get('track')}, Referenz {ref_m.get('car')} @ {ref_m.get('track')}\n", "warn")
        for k, col in enumerate(WORST_COLORS):
            t.tag_configure(f"worst{k}", foreground=col, font=("Segoe UI", 10, "bold"))
        for i, line in enumerate(c["summary"]):
            if i == 0:
                t.insert("end", line + "\n", "b")
            elif i <= len(c["worst"]):
                t.insert("end", "● ", f"worst{i - 1}")
                t.insert("end", line + "\n", "")
            else:
                t.insert("end", "• " + line + "\n", "")
        t.insert("end", "\nBremspunkt/Vollgas: Meter ab Start/Ziel; „früher/später“ = du gegenüber der Referenz. "
                        "Lenkeinschlag mehr als die Referenz = Auto schiebt oder rutscht (Linie oder Setup).\n", "dim")
        sp = compare.ref_setup(self.ref_path)
        own_setup = self.app.setup
        if sp:
            t.insert("end", "\nSetup im Vergleich\n", "h")
            if own_setup is None:
                t.insert("end", f"Referenz-Setup: {sp.name}. Wähle oben dein gefahrenes Setup, dann werden die Unterschiede gezeigt.\n")
            else:
                try:
                    diff = compare.setup_diff(own_setup, compare.load_setup(sp))
                except Exception as e:
                    diff = []
                    t.insert("end", f"Referenz-Setup nicht lesbar: {e}\n", "warn")
                t.insert("end", f"{len(diff)} Unterschiede (Clicks, deins → Referenz), Referenz: {sp.name}\n", "dim")
                for k, a, b in diff:
                    t.insert("end", f"  {k:52s} {str(a):>8s} → {str(b):<8s}\n", "mono")
        t.configure(state="disabled")

        self.tree.delete(*self.tree.get_children())

        def m(v):
            return "–" if v is None else f"{v:.0f} m"
        for sct in c["sections"]:
            tag = ("lost",) if sct["dt"] > 0.05 else ("won",) if sct["dt"] < -0.05 else ()
            self.tree.insert("", "end", iid=str(sct["no"]), tags=tag, values=(
                f"K{sct['no']}", f"{sct['apex'] * 100:.0f} %", f"{sct['dt']:+.2f} s",
                f"{m(sct['brake_o'])} / {m(sct['brake_r'])}", f"{sct['vmin_o']:.0f} / {sct['vmin_r']:.0f} km/h",
                f"{m(sct['gas_o'])} / {m(sct['gas_r'])}", ", ".join(sct["why"])))
        self._plot()

    def _plot(self, xlim=None):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure
        c = self.result
        tr = c["trace"]
        if self.fig is None:
            self.fig = Figure(figsize=(10, 6), dpi=90)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
            NavigationToolbar2Tk(self.canvas, self.plot_frame).update()
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
        fig = self.fig
        fig.clear()
        rows = (("delta", "Δ Zeit [s]"), ("speed", "km/h"), ("gas", "Gas"), ("brake", "Bremse"), ("steer", "Lenkung"))
        has_map = c.get("map") is not None
        gs = fig.add_gridspec(len(rows), 2 if has_map else 1, width_ratios=[2.6, 1] if has_map else None,
                              height_ratios=[1.2, 1.6, 0.8, 0.8, 1.0], wspace=0.06)
        axs = [fig.add_subplot(gs[0, 0])]
        axs += [fig.add_subplot(gs[i, 0], sharex=axs[0]) for i in range(1, len(rows))]
        for ax in axs[:-1]:
            ax.tick_params(labelbottom=False)
        for ax, (key, lab) in zip(axs, rows):
            if key == "delta":
                ax.fill_between(tr.m, 0, tr.delta, where=tr.delta >= 0, color="#c62828", alpha=0.35, lw=0)
                ax.fill_between(tr.m, 0, tr.delta, where=tr.delta < 0, color="#2e7d32", alpha=0.35, lw=0)
                ax.plot(tr.m, tr.delta, color="#333", lw=1)
                ax.axhline(0, color="#999", lw=0.6)
            else:
                ax.plot(tr.m, tr[f"{key}_r"], color="#1c5fd1", lw=1, label="Referenz")
                ax.plot(tr.m, tr[f"{key}_o"], color="#e65100", lw=1, label="Du")
            ax.set_ylabel(lab, fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(alpha=0.25)
        axs[1].legend(fontsize=7, loc="lower right")
        for sct in c["sections"]:
            x = sct["apex"] * c["length"]
            for ax in axs:
                ax.axvline(x, color="#bbb", lw=0.5, ls=":")
            axs[0].text(x, 1.0, f"K{sct['no']}", transform=axs[0].get_xaxis_transform(), fontsize=7, ha="center", va="bottom")
        for k, sct in enumerate(c["worst"]):     # Top-3-Verluste auch in den Diagrammen hinterlegen
            for ax in axs:
                ax.axvspan(sct["x0"] * c["length"], sct["x1"] * c["length"], color=WORST_COLORS[k], alpha=0.08, lw=0)
        axs[-1].set_xlabel("Strecke [m] ab Start/Ziel", fontsize=8)
        if xlim:
            axs[-1].set_xlim(*xlim)
        self.map_ax = None
        if has_map:
            self.map_ax = fig.add_subplot(gs[:, 1])
            self._draw_map(self.map_ax)
        fig.subplots_adjust(left=0.07, right=0.99, top=0.95, bottom=0.08, hspace=0.12)
        if not getattr(self, "_map_click", None):
            self._map_click = self.canvas.mpl_connect("button_press_event", self._on_map_click)
        self.canvas.draw_idle()
        self._draw_big_map()

    # ------------------------------------------------------------------ große Karte (eigenes Fenster)
    def open_big_map(self):
        if not self.result or self.result.get("map") is None:
            messagebox.showinfo("Streckenkarte", "Erst vergleichen – die Karte entsteht aus der Referenzrunde.")
            return
        if self.big_win is not None and self.big_win.winfo_exists():
            self.big_win.deiconify()
            self.big_win.lift()
            return
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure
        w = tk.Toplevel(self)
        w.title("Streckenkarte – Zeitverlust")
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        size = int(min(sw * 0.6, sh * 0.85))
        w.geometry(f"{size}x{size}+{(sw - size) // 2}+{max(0, (sh - size) // 2 - 30)}")
        self.big_fig = Figure(figsize=(8, 8), dpi=100)
        self.big_canvas = FigureCanvasTkAgg(self.big_fig, master=w)
        NavigationToolbar2Tk(self.big_canvas, w).update()
        self.big_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.big_canvas.mpl_connect("button_press_event", self._on_map_click)

        def close():
            self.big_win = self.big_fig = self.big_canvas = self.big_ax = None
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", close)
        w.bind("<Escape>", lambda e: close())
        self.big_win = w
        self._draw_big_map()

    def _draw_big_map(self):
        if self.big_fig is None or not self.result or self.result.get("map") is None:
            return
        f = self.big_fig
        f.clear()
        self.big_ax = f.add_subplot(111)
        self._draw_map(self.big_ax, scale=1.7)
        sel = self.tree.selection()
        sct = next((x for x in self.result["sections"] if sel and str(x["no"]) == sel[0]), None)
        if sct:
            why = ", ".join(sct["why"]) or "kein einzelner deutlicher Unterschied"
            self.big_ax.set_title(f"K{sct['no']}: {sct['dt']:+.2f} s – {why}", fontsize=12, color="#1c5fd1", wrap=True)
        else:
            self.big_ax.set_title(f"Rundenzeit {self.result['total']:+.3f} s gegenüber der Referenz", fontsize=12)
        f.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=0.05)
        self.big_canvas.draw_idle()

    def _draw_map(self, ax, scale: float = 1.0):
        """Streckenkarte: ganze Runde grau, die drei größten Zeitverluste farbig, gewählte Kurve blau umrandet."""
        c = self.result
        x, y = c["map"]
        n = len(x)

        def seg(a, b):
            i0, i1 = int(a * (n - 1)), int(b * (n - 1)) + 1
            return x[i0:i1], y[i0:i1]
        ax.plot(x, y, color="#9e9e9e", lw=5 * scale, solid_capstyle="round", zorder=1)
        ax.plot(x, y, color="#f5f5f5", lw=2.5 * scale, solid_capstyle="round", zorder=2)
        sel = self.tree.selection()
        for sct in c["sections"]:
            if sel and str(sct["no"]) == sel[0]:
                ax.plot(*seg(sct["x0"], sct["x1"]), color="#1c5fd1", lw=11 * scale, alpha=0.35, solid_capstyle="round", zorder=3)
        for k, sct in enumerate(c["worst"]):
            ax.plot(*seg(sct["x0"], sct["x1"]), color=WORST_COLORS[k], lw=6 * scale, solid_capstyle="round", zorder=5 - k)
        span = max(np.ptp(x), np.ptp(y), 1.0)
        cx, cy = np.mean(x), np.mean(y)
        worst = {s["no"]: k for k, s in enumerate(c["worst"])}
        for sct in c["sections"]:
            i = int(sct["apex"] * (n - 1))
            dx, dy = x[i] - cx, y[i] - cy                      # Beschriftung etwas nach außen
            d = max(np.hypot(dx, dy), 1.0)
            tx, ty = x[i] + dx / d * span * 0.06, y[i] + dy / d * span * 0.06
            if sct["no"] in worst:
                k = worst[sct["no"]]
                ax.annotate(f"{k + 1}. K{sct['no']}  {sct['dt']:+.2f} s", (x[i], y[i]), (tx, ty), fontsize=8 * scale, fontweight="bold",
                            color="white", ha="center", va="center", zorder=10,
                            bbox=dict(boxstyle="round,pad=0.25", fc=WORST_COLORS[k], ec="none"),
                            arrowprops=dict(arrowstyle="-", color=WORST_COLORS[k], lw=1))
            else:
                ax.text(tx, ty, f"K{sct['no']}", fontsize=7 * scale, color="#616161", ha="center", va="center", zorder=6)
        # Start/Ziel und Fahrtrichtung
        ax.plot(x[0], y[0], marker="s", color="black", ms=6 * scale, zorder=11)
        j = max(2, n // 60)
        ax.annotate("", (x[j], y[j]), (x[0], y[0]), zorder=11,
                    arrowprops=dict(arrowstyle="-|>", color="black", lw=1.5 * scale, mutation_scale=12 * scale))
        ax.text(x[0], y[0] - span * 0.05, "Start/Ziel", fontsize=7 * scale, ha="center", va="top", zorder=11)
        ax.margins(0.12)
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.text(0.5, 0.0, "Rot / Orange / Gelb = 1. / 2. / 3. größter Zeitverlust · Klick = Kurve zeigen", transform=ax.transAxes,
                fontsize=7 * min(scale, 1.4), color="#616161", ha="center", va="top")

    def _on_map_click(self, e):
        """Klick auf die Karte: nächstgelegene Kurve wählen (Tabelle + Zoom der Diagramme)."""
        if e.inaxes is None or e.inaxes not in (getattr(self, "map_ax", None), self.big_ax) or not self.result:
            return
        if e.canvas.toolbar is not None and e.canvas.toolbar.mode:   # Zoom/Verschieben aktiv -> kein Auswählen
            return
        x, y = self.result["map"]
        i = int(np.argmin((x - e.xdata) ** 2 + (y - e.ydata) ** 2))
        p = i / (len(x) - 1)
        sct = next((s for s in self.result["sections"] if s["x0"] <= p < s["x1"]), None)
        if sct:
            self.tree.selection_set(str(sct["no"]))
            self.tree.see(str(sct["no"]))

    def _zoom_section(self, _e=None):
        sel = self.tree.selection()
        if not sel or not self.result:
            return
        sct = next(x for x in self.result["sections"] if str(x["no"]) == sel[0])
        L = self.result["length"]
        self._plot((max(0, sct["x0"] * L - 100), min(L, sct["x1"] * L + 50)))


def newest_setup(meta: dict | None) -> Path | None:
    """Zuletzt gespeicherte .json im Setup-Ordner von Auto/Strecke (z. B. das eben gesicherte Aggressive-Preset)."""
    if not meta:
        return None
    folder = setup_folder(meta)
    if folder == acc_dir() / "Setups":
        return None
    try:
        files = sorted(folder.glob("*.json"), key=lambda q: q.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return files[0] if files else None


def car_canvas(master, p: dict) -> tk.Canvas:
    """Draufsicht Auto wie im Report: Rad eingefärbt nach nötiger Druckänderung (rot runter, blau rauf, grün passt)."""
    W, H = 470, 300
    cv = tk.Canvas(master, width=W, height=H, background="white", highlightthickness=0)
    cx = W / 2
    cv.create_text(cx, 10, text="▲ VORNE", fill="#666", font=("Segoe UI", 8))
    cv.create_polygon(cx - 58, 48, cx - 60, 30, cx - 34, 20, cx + 34, 20, cx + 60, 30, cx + 58, 48, cx + 64, 240,
                      cx + 62, 268, cx + 34, 272, cx - 34, 272, cx - 62, 268, cx - 64, 240,
                      smooth=True, fill="#e9ecef", outline="#6c757d", width=2)
    cv.create_polygon(cx - 36, 100, cx, 90, cx + 36, 100, cx + 31, 158, cx, 166, cx - 31, 158, fill="#adb5bd", smooth=True)
    cv.create_rectangle(cx - 54, 258, cx + 54, 268, fill="#6c757d", outline="")
    pos = {"fl": (cx - 76, 56), "fr": (cx + 76, 56), "rl": (cx - 80, 200), "rr": (cx + 80, 200)}
    names = dict(zip(WHEELS, K.WHEEL_DE))
    for w, (x, y) in pos.items():
        fill, tc, txt = report.wheel_style(p["clicks"][w])
        cv.create_rectangle(x - 12, y - 25, x + 12, y + 25, fill=fill, outline="#333", width=2)
        right = x > cx
        tx, anc = (x + 22, "w") if right else (x - 22, "e")
        cv.create_text(tx, y - 14, text=names[w], anchor=anc, font=("Segoe UI", 10, "bold"), fill="#222")
        cv.create_text(tx, y + 3, text=txt, anchor=anc, font=("Segoe UI", 11, "bold"), fill=tc)
        cv.create_text(tx, y + 20, text=f"Ø {p['avg'][w]:.2f} psi (Δ {p['diff'][w]:+.2f})", anchor=anc,
                       font=("Segoe UI", 8), fill="#555")
    cv.create_text(cx, H - 10, text="rot = zu hoch → runter · blau = zu niedrig → rauf · grün = passt",
                   fill="#777", font=("Segoe UI", 8))
    return cv


def _text(master) -> ScrolledText:
    t = ScrolledText(master, wrap="word", font=("Segoe UI", 10), padx=8, pady=6, height=12)
    t.tag_configure("h", font=("Segoe UI", 12, "bold"), spacing1=8, spacing3=4)
    t.tag_configure("b", font=("Segoe UI", 10, "bold"))
    t.tag_configure("dim", foreground="#666")
    t.tag_configure("warn", foreground="#b00")
    t.tag_configure("mono", font=("Consolas", 10))
    t.tag_configure("monob", font=("Consolas", 10, "bold"))
    t.tag_configure("us", foreground="#1c5fd1", font=("Consolas", 10, "bold"))
    t.tag_configure("os", foreground="#c62828", font=("Consolas", 10, "bold"))
    t.configure(state="disabled")
    return t


class App(tk.Tk):
    def __init__(self, ld: str | None = None, setup: str | None = None):
        super().__init__()
        self.title("ACC Setup-Analyzer")
        ico = Path(__file__).resolve().parent / "assets" / "icon.ico"
        try:
            self.iconbitmap(default=str(ico))      # Fenster- und Taskleisten-Icon (Rennstrecke)
        except tk.TclError:
            pass
        self.geometry("1150x880")
        self.minsize(900, 650)
        self.q: queue.Queue = queue.Queue()
        self.df = self.meta = self.evals = self.setup = None
        self.html: Path | None = None
        self.sess_vars: dict[int, tk.StringVar] = {}
        self.mode_tabs: dict[str, ModeTab] = {}
        self.busy = False
        self.cond_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.cond_user: dict[str, set] = {}      # vom Benutzer geänderte Felder (überschreiben live/abgeleitet)
        self._filling = False
        self.live_var = tk.BooleanVar(value=True)
        self.live_status = tk.StringVar(value="")
        self.logger: livelog.LiveLogger | None = None
        self.live_q: queue.Queue = queue.Queue()
        self.live_busy = False
        self.live_seen = (None, -1)        # (Session-Schlüssel, Runden) der letzten Live-Auswertung
        self.live_html: Path | None = None
        self.live_meta: dict | None = None
        self.live_again = False             # während einer Live-Auswertung angefordert -> danach erneut

        self.ld_var = tk.StringVar(value=ld or "")
        self.setup_var = tk.StringVar(value=setup or "")
        self.status = tk.StringVar(value="Aufnahme (.ld) wählen und „Laden“ klicken.")
        self._build()
        if not ld:
            n = newest_ld(acc_dir() / "MoTeC")
            if n:
                self.ld_var.set(str(n))
                self.status.set(f"Neueste Aufnahme vorausgewählt: {n.name} – „Laden“ klicken.")
        self.after(100, self._poll)
        self.toggle_live()
        self.protocol("WM_DELETE_WINDOW", self._close)
        if ld:
            self.after(200, self.load)

    # ------------------------------------------------------------------ Layout
    def _build(self):
        pad = dict(padx=6, pady=4)
        top = ttk.LabelFrame(self, text="Dateien")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="MoTeC-Aufnahme (.ld):").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.ld_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(top, text="Durchsuchen …", command=self.pick_ld).grid(row=0, column=2, **pad)
        ttk.Label(top, text="Gefahrenes Setup (.json, optional):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.setup_var).grid(row=1, column=1, sticky="ew", **pad)
        f = ttk.Frame(top)
        f.grid(row=1, column=2, **pad)
        ttk.Button(f, text="Durchsuchen …", command=self.pick_setup).pack(side="left")
        ttk.Button(f, text="✕", width=3, command=self.clear_setup).pack(side="left", padx=(4, 0))
        row = ttk.Frame(top)
        row.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        self.btn_load = ttk.Button(row, text="Laden", command=self.load)
        self.btn_load.pack(side="left")
        top.columnconfigure(1, weight=1)

        self.sess_frame = ttk.LabelFrame(self, text="Sessions in der Aufnahme – Zuordnung Rennen / Quali / – (nicht auswerten)")
        self.sess_frame.pack(fill="x", **pad)
        ttk.Label(self.sess_frame, text="– noch keine Aufnahme geladen –").pack(anchor="w", **pad)

        cf = ttk.LabelFrame(self, text="Bedingungen (für Sprit und Dateiname) – nur live aus ACC, sonst leer; selbst eintragbar")
        cf.pack(fill="x", **pad)
        fields = (("air", "Luft °C", 5), ("road", "Asphalt °C", 5), ("minutes", "Dauer min", 5), ("quick", "schnelle Runden", 4))
        for mi, (mode, title) in enumerate((("race", "Rennen"), ("quali", "Quali"))):
            ttk.Label(cf, text=title + ":", font=("Segoe UI", 9, "bold")).grid(row=mi, column=0, sticky="w", padx=6, pady=1)
            self.cond_vars[mode] = {}
            col = 1
            for key, lab, w in fields:
                if key == "quick" and mode != "quali":
                    continue
                v = tk.StringVar()
                v.trace_add("write", lambda *_, m=mode, k=key: self._cond_edited(m, k))
                self.cond_vars[mode][key] = v
                ttk.Label(cf, text=lab).grid(row=mi, column=col, sticky="e", padx=(10, 2))
                ttk.Entry(cf, textvariable=v, width=w).grid(row=mi, column=col + 1, sticky="w")
                col += 2
            src = tk.StringVar()
            self.cond_vars[mode]["_src"] = src
            ttk.Label(cf, textvariable=src, foreground="#666").grid(row=mi, column=9, sticky="w", padx=10)
        ttk.Button(cf, text="Übernehmen", command=self.run_analysis).grid(row=0, column=10, rowspan=2, padx=10)
        cf.columnconfigure(9, weight=1)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, **pad)
        self.overview = _text(self.nb)
        self.nb.add(self.overview, text="Übersicht")

        lt = ttk.Frame(self.nb)
        cols = ("sess", "num", "kind", "time", "delta", "valid", "fl", "fr", "rl", "rr", "temp")
        heads = ("Session", "Rd.", "Typ", "Zeit", "Δ Beste", "gültig", "Druck VL", "VR", "HL", "HR", "Reifentemp.")
        widths = (150, 45, 85, 85, 80, 55, 70, 55, 55, 55, 85)
        self.tree = ttk.Treeview(lt, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center" if c not in ("sess", "kind") else "w")
        self.tree.tag_configure("best", background="#d8f5d0")
        self.tree.tag_configure("invalid", foreground="#999")
        self.tree.tag_configure("unused", foreground="#b0b0b0")
        sb = ttk.Scrollbar(lt, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.laps_tab = lt
        self.nb.add(lt, text="Runden")
        self.live_tab = LiveTab(self.nb, self)
        self.nb.add(self.live_tab, text="🔴 Live")
        self.compare_tab = CompareTab(self.nb, self)
        self.nb.add(self.compare_tab, text="⚖ Vergleich")
        self.live_tab.show_intro()

        bot = ttk.Frame(self)
        bot.pack(fill="x", **pad)
        self.btn_report = ttk.Button(bot, text="Ausführlichen Report im Browser öffnen", command=self.open_report, state="disabled")
        self.btn_report.pack(side="right")
        ttk.Checkbutton(bot, text="Live-Mitschnitt", variable=self.live_var, command=self.toggle_live).pack(side="right", padx=8)
        ttk.Label(bot, textvariable=self.live_status, foreground="#0a6").pack(side="right", padx=8)
        ttk.Label(bot, textvariable=self.status).pack(side="left")

    # ------------------------------------------------------------------ Live-Mitschnitt
    def toggle_live(self):
        if self.live_var.get() and self.logger is None:
            self.logger = livelog.LiveLogger()
            self.logger.start()
        elif not self.live_var.get() and self.logger is not None:
            self.logger.stop()
            self.logger = None
            self.live_status.set("")

    def _close(self):
        if self.logger is not None:
            self.logger.stop()
        self.destroy()

    # ------------------------------------------------------------------ Bedingungen
    def _cond_edited(self, mode, key):
        if not self._filling:
            self.cond_user.setdefault(mode, set()).add(key)

    def _cond_overrides(self) -> dict:
        """Nur die vom Benutzer geänderten Felder – der Rest kommt je Auswertung neu aus Live/Ableitung."""
        out: dict = {}
        for mode, keys in self.cond_user.items():
            for key in keys:
                txt = self.cond_vars[mode][key].get().strip().replace(",", ".")
                try:
                    val = float(txt) if key in ("air", "road") else int(float(txt))
                except ValueError:
                    continue
                out.setdefault(mode, {})[key] = val
        return out

    def _fill_cond(self):
        self._filling = True
        try:
            for mode, vars_ in self.cond_vars.items():
                e = (self.evals or {}).get(mode)
                c = e["cond"] if e else {}
                for key, v in vars_.items():
                    if key == "_src":
                        continue
                    x = c.get(key)
                    v.set("" if x is None else (f"{x:.0f}" if isinstance(x, float) else str(x)))
                src = dict(c.get("src") or {})
                for k in self.cond_user.get(mode, ()):
                    src[k] = "Eingabe"
                names = {"air": "Luft", "road": "Asphalt", "minutes": "Dauer"}
                if not e:
                    txt = ""
                elif not src:
                    txt = "kein Live-Mitschnitt – bitte selbst eintragen (sonst Sprit unverändert, Dateiname ohne °C)"
                else:
                    txt = "; ".join(f"{names.get(k, k)}: {'live aus ACC' if v == 'live' else v}" for k, v in src.items())
                vars_["_src"].set(txt)
        finally:
            self._filling = False

    # ------------------------------------------------------------------ Dateiauswahl
    def pick_ld(self):
        cur = Path(self.ld_var.get()) if self.ld_var.get() else None
        init = cur.parent if cur and cur.parent.is_dir() else acc_dir() / "MoTeC"
        p = filedialog.askopenfilename(title="MoTeC-Aufnahme wählen", initialdir=str(init) if init.is_dir() else None,
                                       filetypes=[("ACC MoTeC", "*.ld"), ("Alle Dateien", "*.*")])
        if p:
            self.ld_var.set(p)
            self.load()

    def pick_setup(self):
        cur = Path(self.setup_var.get()) if self.setup_var.get() else None
        live_shown = self.nb.select() == str(self.live_tab)
        init = cur.parent if cur and cur.parent.is_dir() else setup_folder(
            self.live_meta if live_shown and self.live_meta else self.meta or self.live_meta)
        p = filedialog.askopenfilename(title="Gefahrenes Setup wählen", initialdir=str(init) if init.is_dir() else None,
                                       filetypes=[("ACC Setup", "*.json"), ("Alle Dateien", "*.*")])
        if p:
            self.use_setup(p)

    def use_setup(self, p: str):
        self.setup_var.set(p)
        self._setup_changed()

    def clear_setup(self):
        self.setup_var.set("")
        self._setup_changed()

    def _setup_changed(self):
        """Setup gewechselt -> MoTeC- und Live-Auswertung neu (sonst fehlen dort Änderungen und Speichern)."""
        if self.df is not None:
            self.run_analysis()
        if self.live_meta is not None:
            self.run_live(manual=True)

    # ------------------------------------------------------------------ Hintergrundarbeit
    def _work(self, fn, done, msg):
        if self.busy:
            return
        self.busy = True
        self.status.set(msg)
        self.config(cursor="watch")
        self.btn_load.state(["disabled"])
        gc.collect()   # alte Tk-Variablen hier im Haupt-Thread freigeben, nicht zufällig im Worker-Thread

        def run():
            try:
                self.q.put((done, fn(), None))
            except Exception as e:
                self.q.put((done, None, (e, traceback.format_exc())))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, val, err = self.q.get_nowait()
                self.busy = False
                self.config(cursor="")
                self.btn_load.state(["!disabled"])
                if err:
                    self.status.set("Fehler – Details im Dialog.")
                    messagebox.showerror("Fehler", f"{err[0]}\n\n{err[1][-1500:]}")
                else:
                    done(val)
        except queue.Empty:
            pass
        self._poll_live()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ Laden
    def load(self):
        p = Path(self.ld_var.get().strip().strip('"'))
        if not p.is_file() or p.suffix.lower() != ".ld":
            messagebox.showwarning("Keine Aufnahme", "Bitte eine .ld-Datei aus  Dokumente\\Assetto Corsa Competizione\\MoTeC  wählen.")
            return
        if not p.with_suffix(".ldx").exists():
            messagebox.showinfo("Hinweis", "Die passende .ldx-Datei (Rundenmarken) fehlt neben der .ld – Runden werden ungenau.")

        def fn():
            from .ldimport import load_ld
            df, meta = load_ld(p)
            livelog.attach(meta)
            return df, meta
        self._work(fn, self._loaded, f"Lade {p.name} …")

    def _loaded(self, val):
        self.df, self.meta = val
        self.cond_user = {}
        if self.meta.get("moving") is False:
            messagebox.showinfo("Keine Fahrt", "In dieser Aufnahme hat sich das Auto nicht bewegt.")
            return
        self._show_sessions()
        self.run_analysis()

    def _show_sessions(self):
        for w in self.sess_frame.winfo_children():
            w.destroy()
        ss = self.meta.get("sessions") or []
        groups = analysis.default_groups(self.meta)
        default = {sid: m for m, ids in groups.items() for sid in (ids or [])}
        label = {v: k for k, v in CHOICES.items()}
        self.sess_vars = {}
        hdr = ("Auswerten als", "Session", "Beginn", "Dauer", "Runden", "gültig", "Beste", "Median")
        for c, h in enumerate(hdr):
            ttk.Label(self.sess_frame, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=c, sticky="w", padx=6)
        for r, s in enumerate(ss, 1):
            v = tk.StringVar(value=label.get(default.get(s["id"]), "–"))
            self.sess_vars[s["id"]] = v
            ttk.Combobox(self.sess_frame, textvariable=v, values=list(CHOICES), width=8, state="readonly").grid(row=r, column=0, padx=6, pady=1)
            vals = (report.session_label(s), report._clock(s["start"]), report._clock(s["dur"]) + " min",
                    str(s["laps"]), str(s["laps_valid"]), report._lt(s["best"]), report._lt(s["median"]))
            for c, x in enumerate(vals, 1):
                ttk.Label(self.sess_frame, text=x).grid(row=r, column=c, sticky="w", padx=6)
        ttk.Button(self.sess_frame, text="Neu auswerten", command=self.run_analysis).grid(
            row=1, column=len(hdr), rowspan=max(1, len(ss)), padx=16, sticky="n")
        if not ss:
            ttk.Label(self.sess_frame, text="(keine Sessions erkannt)").grid(row=1, column=1, sticky="w")

    # ------------------------------------------------------------------ Analyse
    def _groups(self) -> dict | None:
        if not self.sess_vars:
            return None
        g: dict = {}
        for sid, v in self.sess_vars.items():
            m = CHOICES.get(v.get())
            if m:
                g.setdefault(m, []).append(sid)
        return {m: g[m] for m in ("race", "quali") if m in g}   # Reihenfolge der Reiter: Rennen, Quali

    def run_analysis(self):
        if self.df is None:
            return
        groups = self._groups()
        if groups is not None and not groups:
            messagebox.showwarning("Keine Session", "Bitte mindestens eine Session als Rennen oder Quali auswerten.")
            return
        sp = self.setup_var.get().strip().strip('"')
        ld = Path(self.ld_var.get().strip().strip('"'))
        df, meta = self.df, self.meta
        over = self._cond_overrides()

        def fn():
            setup = setupio.load(sp) if sp else None
            evals = pipeline.evaluate(df, meta, groups, setup, over, sp or None)
            _, html, _ = pipeline.write_report(df, meta, evals, docs_dir() / "ACC-Analyse", ld.stem)
            return evals, setup, html
        self._work(fn, self._analyzed, "Analysiere …")

    def _analyzed(self, val):
        self.evals, self.setup, self.html = val
        self.btn_report.state(["!disabled"])
        # Reiter je Auswertung neu aufbauen (zwischen Übersicht und Runden)
        for t in self.mode_tabs.values():
            self.nb.forget(t)
            t.destroy()
        self.mode_tabs = {}
        for i, (mode, e) in enumerate(self.evals.items(), 1):
            tab = ModeTab(self.nb, self, mode)
            self.nb.insert(i, tab, text=TAB_TITLE.get(mode, mode))
            tab.fill(e["res"], e["plan"], e.get("cond"))
            self.mode_tabs[mode] = tab
        self._fill_cond()
        self._fill_overview()
        self._fill_laps()
        self.compare_tab.own_loaded()
        n = {analysis.MODES.get(m, m): sum(1 for f in e["res"].findings if f.level == "action") for m, e in self.evals.items()}
        self.status.set("Fertig – Empfehlungen: " + ", ".join(f"{k} {v}" for k, v in n.items()) + f".  Report: {self.html}")

    # ------------------------------------------------------------------ Anzeige
    def _fill_overview(self):
        t = self.overview
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", f"{self.meta.get('car', '?')} @ {self.meta.get('track', '?')}\n", "h")
        if self.setup is not None:
            warn = setupio.check_car(self.setup, self.meta)
            if warn:
                t.insert("end", warn + "\n", "warn")
        results = {m: e["res"] for m, e in self.evals.items()}
        modes = list(results)
        names = dict(zip(WHEELS, K.WHEEL_DE))
        t.insert("end", "Vergleich der Auswertungen\n", "h")
        t.insert("end", f"{'':34s}" + "".join(f"{analysis.MODES.get(m, m):>14s}" for m in modes) + "\n", "b")

        def row(label, fn):
            t.insert("end", f"{label:34s}" + "".join(f"{fn(results[m].stats):>14s}" for m in modes) + "\n", "mono")
        row("Gültige Runden im Ø", lambda s: str((s.get("pressure") or {}).get("n_laps", s["laps_valid"])))
        for w in WHEELS:
            row(f"Ø Druck {names[w]} [psi]", lambda s, w=w: report._num((s.get("pressure") or {}).get("avg", {}).get(w, float("nan")), ".2f"))
        for w in WHEELS:
            row(f"Ø Reifentemp. {names[w]} [°C]", lambda s, w=w: report._num((s.get("temps") or {}).get("tyre", {}).get(w, float("nan"))))
        row("Ø Bremse Spitze v / h [°C]", lambda s: report._brake_axles(s.get("temps")))
        row("Druck-Clicks VL/VR/HL/HR", lambda s: "/".join(
            f"{c:+d}" if c else "0" for c in ((s.get("pressure") or {}).get("clicks") or {w: 0 for w in WHEELS}).values()))
        t.tag_configure("mono", font=("Consolas", 10))
        t.tag_configure("b", font=("Consolas", 10, "bold"))

        t.insert("end", "\nSessions\n", "h")
        smode = report.session_modes(results)
        first = next(iter(results.values()))
        for s in first.stats.get("sessions") or []:
            m = analysis.MODES.get(smode.get(s["id"]), "–")
            p = " / ".join(report._num(s["p"][w], ".2f") for w in WHEELS)
            tc = " / ".join(report._num(s["tc"][w]) for w in WHEELS)
            t.insert("end", f"{report.session_label(s):22s} → {m:7s} {s['laps_valid']:2d} gültige Rd., Beste {report._lt(s['best'])}"
                            f"   Ø Druck {p} psi   Ø Reifen {tc} °C\n", "mono")
        if self.setup is not None:
            t.insert("end", "\nGefahrenes Setup\n", "h")
            t.insert("end", setupio.summary(self.setup) + "\n", "mono")
        t.configure(state="disabled")

    def _fill_laps(self):
        self.tree.delete(*self.tree.get_children())
        results = {m: e["res"] for m, e in self.evals.items()}
        smode = report.session_modes(results)
        res = next(iter(results.values()))
        sess = {s["id"]: s for s in res.stats.get("sessions") or []}
        lp = res.stats.get("lap_p", {})
        laps = res.laps
        best = {sid: g[g.clean].lap_s.min() for sid, g in laps.groupby("session") if g.clean.any()}
        for r in laps.itertuples():
            s = sess.get(r.session)
            b = best.get(r.session)
            is_best = bool(r.clean and b is not None and r.lap_s == b)
            delta = "Bestzeit" if is_best else (f"+{r.lap_s - b:.2f}" if r.clean and b is not None else "")
            p = lp.get(int(r.lap))
            pv = [f"{p[f'p_{w}']:.2f}" if p else "" for w in WHEELS]
            temp = f"{p['tc']:.0f} °C" if p and p["tc"] == p["tc"] else ""
            used = r.session in smode
            tags = ("best",) if is_best else ("unused",) if not used else () if r.clean else ("invalid",)
            name = (report.session_label(s) + (f" ({analysis.MODES[smode[r.session]]})" if used else "")) if s else "-"
            self.tree.insert("", "end", values=(name, r.num, r.kind, report._lt(r.lap_s), delta,
                                                "ja" if r.clean else "nein", *pv, temp), tags=tags)

    # ------------------------------------------------------------------ Live-Auswertung (ohne MoTeC)
    def _poll_live(self):
        try:
            while True:
                kind, val = self.live_q.get_nowait()
                self.live_busy = False
                if self.live_again:
                    self.live_again = False
                    self.after(10, lambda: self.run_live(manual=True))
                if kind == "err":
                    self.live_tab.info.set(f"Live-Auswertung fehlgeschlagen: {val[0]}")
                elif kind == "wait":
                    self.live_tab.info.set(val)
                else:
                    self._live_done(val)
        except queue.Empty:
            pass
        lg = self.logger
        if lg is None:
            return
        self.live_status.set(lg.state)
        mon, tab = lg.monitor, self.live_tab
        for k, v in tab.bal_vars.items():
            v.set(mon.counts_text(*k))
        kind, sev = mon.active(time.time())
        tab.bal_now.set("" if not kind else f"{'▼ UNTERSTEUERN' if kind == 'us' else '▲ ÜBERSTEUERN'} {mon.value:+.1f}°")
        rec = lg.recorder
        if self.live_tab.auto_var.get() and not self.live_busy and rec.laps_done >= 2 and (rec.key, rec.laps_done) != self.live_seen:
            self.run_live()

    def run_live(self, manual: bool = False):
        if self.live_busy:
            self.live_again = self.live_again or manual
            return
        if self.logger is None:
            if manual:
                messagebox.showinfo("Live-Mitschnitt aus", "Unten „Live-Mitschnitt“ einschalten und in ACC fahren.")
            return
        rec = self.logger.recorder
        self.live_seen = (rec.key, rec.laps_done)
        data, info = rec.snapshot()
        if len(data) < 500:
            self.live_tab.info.set("Noch keine Live-Daten – in ACC eine Session starten und losfahren.")
            return
        sp = self.setup_var.get().strip().strip('"')
        choice = LiveTab.MODE_CHOICES.get(self.live_tab.mode_var.get())
        self.live_busy = True
        self.live_tab.info.set("Live-Auswertung läuft …")

        def run():
            try:
                df, meta = livesession.build(data, info)
                if not any(l["valid"] for l in meta["laps_info"]):
                    self.live_meta = meta
                    self.live_q.put(("wait", f"{meta['car']} @ {meta['track']}: noch keine gültige gezeitete Runde "
                                             f"({len(meta['laps_info'])} abgeschlossen) – weiterfahren."))
                    return
                mode = choice or livesession.default_mode(meta)
                setup = setupio.load(sp) if sp else None
                ev = pipeline.evaluate(df, meta, {mode: [0]}, setup, None, sp or None)
                stem = f"Live_{meta['track']}_{meta['car']}_{time.strftime('%Y-%m-%d_%H-%M', time.localtime(meta['rec_start']))}"
                _, html, _ = pipeline.write_report(df, meta, ev, docs_dir() / "ACC-Analyse", stem)
                self.live_q.put(("ok", (meta, mode, ev, setup, html)))
            except Exception as e:
                self.live_q.put(("err", (e, traceback.format_exc())))
        threading.Thread(target=run, daemon=True).start()

    def _live_done(self, val):
        meta, mode, ev, setup, html = val
        self.live_html = html
        self.live_meta = meta
        e = ev[mode]
        tab = self.live_tab
        tab.mode = mode
        tab.fill(e["res"], e["plan"], e["cond"], evals=ev, setup=setup)
        tab.btn_report.state(["!disabled"])
        s = meta["sessions"][0]
        txt = (f"Stand {time.strftime('%H:%M:%S')}: {meta['car']} @ {meta['track']} · {s['type']} · "
               f"{s['laps']} Runden, {s['laps_valid']} gültig, Beste {report._lt(s['best'])} · ausgewertet als {analysis.MODES[mode]}")
        warn = setupio.check_car(setup, meta) if setup is not None else None
        tab.info.set(txt + (f"\n⚠ {warn}" if warn else ""))

    def apply_balance_settings(self):
        s = dict(livebalance.load_settings(), **self.live_tab.balance_settings())
        livebalance.save_settings(s)
        if self.logger is not None:
            self.logger.monitor.s.update(s)

    def test_beep(self, kind: str):
        mon = self.logger.monitor if self.logger is not None else livebalance.BalanceMonitor(livebalance.load_settings())
        mon.test_beep(kind)

    def reset_balance(self):
        if self.logger is not None:
            self.logger.monitor.reset_counts()

    def open_live_report(self):
        if self.live_html and self.live_html.exists():
            try:
                os.startfile(str(self.live_html))
            except (AttributeError, OSError):
                webbrowser.open(self.live_html.as_uri())

    # ------------------------------------------------------------------ Aktionen
    def open_report(self):
        if self.html and self.html.exists():
            try:
                os.startfile(str(self.html))  # Windows
            except (AttributeError, OSError):
                webbrowser.open(self.html.as_uri())


def main(ld: str | None = None, setup: str | None = None):
    try:   # eigenes Taskleisten-Icon statt dem von Python (beim Start ohne .exe)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ACC.SetupAnalyzer")
    except Exception:
        pass
    App(ld, setup).mainloop()


if __name__ == "__main__":
    main()
