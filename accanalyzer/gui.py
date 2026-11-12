"""Einfaches Fenster: MoTeC-Aufnahme + (optional) Setup wählen, Sessions Rennen/Quali zuordnen, Empfehlungen ansehen,
je ein optimiertes Setup für Rennen und Quali speichern.

Start:  python -m accanalyzer   (oder Doppelklick auf die .exe)
"""
from __future__ import annotations

import gc
import os
import queue
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import analysis, livelog, pipeline, report, setupio
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

    def __init__(self, master, app: "App", mode: str):
        super().__init__(master)
        self.app, self.mode = app, mode
        self.change_vars: list[tuple[tk.BooleanVar, dict]] = []
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True)
        self.txt = _text(pw)
        pw.add(self.txt, weight=3)
        self.setup_box = ttk.LabelFrame(pw, text=f"Setup-Datei {analysis.MODES.get(mode, '')}")
        pw.add(self.setup_box, weight=2)

    def fill(self, res, plan, cond=None):
        self.cond = cond
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
            ttk.Label(box, text="Kein Setup gewählt. Wähle oben unter „Gefahrenes Setup“ die .json-Datei, mit der du gefahren bist "
                                "(Dokumente\\Assetto Corsa Competizione\\Setups\\<Auto>\\<Strecke>), dann erscheinen hier die "
                                "konkreten Änderungen und du kannst ein eigenes Setup für diese Auswertung speichern.",
                      wraplength=950, justify="left").pack(anchor="w", padx=10, pady=8)
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
        names = pipeline.variant_names(self.app.evals, self.app.setup_var.get().strip().strip('"'))
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
            setupio.save(setupio.apply_items(self.app.setup, sel), p)
        except Exception as e:
            messagebox.showerror("Speichern fehlgeschlagen", str(e))
            return
        self.app.status.set(f"Gespeichert: {p}")
        messagebox.showinfo("Gespeichert", f"Setup gespeichert:\n{p}\n\n"
                                           "In ACC unter Setup → Laden auswählen (ggf. Setup-Liste neu öffnen).")


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
        init = cur.parent if cur and cur.parent.is_dir() else setup_folder(self.meta)
        p = filedialog.askopenfilename(title="Gefahrenes Setup wählen", initialdir=str(init) if init.is_dir() else None,
                                       filetypes=[("ACC Setup", "*.json"), ("Alle Dateien", "*.*")])
        if p:
            self.setup_var.set(p)
            if self.df is not None:
                self.run_analysis()

    def clear_setup(self):
        self.setup_var.set("")
        if self.df is not None:
            self.run_analysis()

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
        if self.logger is not None:
            self.live_status.set(self.logger.state)
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
