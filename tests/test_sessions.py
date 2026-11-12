"""Regressionstest Session-/Rundenerkennung mit der Beispielaufnahme (Silverstone, 3× Quali + Rennen).

Ausführen:  python -m pytest tests   (wird übersprungen, wenn die Beispieldatei fehlt)
"""
from pathlib import Path

import pytest

from accanalyzer import analysis, setupio
from accanalyzer.ldimport import load_ld

EX = Path(__file__).resolve().parent.parent / "exmaple_files_for_testings"
LD = EX / "Silverstone-mclaren_720s_gt3_evo-0-2026.11.10-21.46.29.ld"
SETUP = EX / "20C_24C_40F.json"

pytestmark = pytest.mark.skipif(not LD.exists(), reason="Beispielaufnahme fehlt")


@pytest.fixture(scope="module")
def loaded():
    return load_ld(LD)


def test_sessions(loaded):
    _, meta = loaded
    ss = meta["sessions"]
    assert [s["type"] for s in ss] == ["Training/Quali", "Rennen"] * 3
    assert [s["laps_valid"] for s in ss] == [1, 11, 2, 7, 2, 10]
    assert all(1000 < s["dur"] < 1500 for s in ss if s["type"] == "Rennen")   # ca. 20 min + Auslaufrunde


def test_lap_kinds(loaded):
    _, meta = loaded
    laps = meta["laps_info"]
    assert all(l["dur"] < 150 for l in laps if l["kind"] != "Box/Stopp")   # keine 6–12-min-"Runden" über Session-Grenzen
    races = [s["id"] for s in meta["sessions"] if s["type"] == "Rennen"]
    for sid in races:
        assert next(l for l in laps if l["session"] == sid)["kind"] == "Start"
    assert sum(l["kind"] == "Box/Stopp" for l in laps) == 1      # Return-to-Pit in S3 (mit Stillstand -> eigene Zeile)
    last = laps[-1]
    assert last["kind"] == "gezeitet" and last["valid"]          # letzte Runde endet genau an der Ziellinie (TIME-Kanal)


def test_default_selection_and_setup(loaded):
    df, meta = loaded
    res = analysis.analyze(df, meta)
    assert res.stats["selected"] == [1, 3, 5]
    assert res.stats["laps_valid"] == 28
    assert "S6 Rd. 4" in next(f.title for f in res.findings if f.title.startswith("Beste Runde"))
    setup = setupio.load(str(SETUP))
    assert setupio.check_car(setup, meta) is None
    changes, _, _ = setupio.collect_changes(res)
    items, _ = setupio.plan_changes(setup, changes)
    new = setupio.apply_items(setup, items)
    assert new["carName"] == setup["carName"]
    for it in items:
        assert it["old"] != it["new"]


def test_race_and_quali_groups(loaded):
    df, meta = loaded
    assert analysis.default_groups(meta) == {"race": [1, 3, 5], "quali": [0, 2, 4]}
    setup = setupio.load(str(SETUP))
    from accanalyzer import pipeline
    ev = pipeline.evaluate(df, meta, None, setup)
    q = ev["quali"]["res"].stats
    assert q["mode"] == "quali"
    assert q["pressure"]["excluded_sessions"] == [0]          # S1 liegt ~2 psi unter S3/S5 -> Ausreißer
    assert q["pressure"]["n_laps"] == 4                         # S3 + S5, inkl. 1. Runde nach der Outlap
    assert all(70 < v < 95 for v in q["temps"]["tyre"].values())
    qi = ev["quali"]["plan"]["items"]
    assert qi and all(it["new"] < it["old"] for it in qi if it["p"] == "basicSetup.tyres.tyrePressure")
    assert not any(it["p"] == "basicSetup.strategy.fuel" for it in qi)   # Sprit steckt in den Varianten
    assert pipeline.setup_name(None, None, SETUP, "quali") == "20C_24C_40F_quali.json"


def test_conditions_and_fuel_variants(loaded, tmp_path):
    df, meta = loaded
    meta = dict(meta, sessions=[dict(s) for s in meta["sessions"]])
    from accanalyzer import pipeline
    setup = setupio.load(str(SETUP))
    # Ohne Live-Mitschnitt: KEINE Schätzung von Luft/Asphalt/Dauer
    ev = pipeline.evaluate(df, meta, None, setup, None, SETUP)
    for m in ("race", "quali"):
        c = ev[m]["cond"]
        assert c["air"] is None and c["road"] is None and c["minutes"] is None
    rv = {v["key"]: v for v in ev["race"]["plan"]["variants"]}
    qv = {v["key"]: v for v in ev["quali"]["plan"]["variants"]}
    assert set(qv) == {"quali", "quali_kurz"}
    assert rv["race"]["item"] is None and qv["quali"]["item"] is None       # Dauer unbekannt -> Sprit unverändert
    assert qv["quali_kurz"]["fuel"] == 20                                  # (3 + Out + In + 1 Reserve) × 3.24 l
    names = pipeline.variant_names(ev, SETUP)
    assert names["quali_kurz"] == "20C_24C_40F_quali_kurz.json"            # ohne Temperaturen: Originalname + Variante
    # Eigene Eingabe
    over = {"race": {"air": 20, "road": 27, "minutes": 20}, "quali": {"air": 21, "road": 31, "minutes": 15}}
    ev2 = pipeline.evaluate(df, meta, None, setup, over, SETUP)
    assert ev2["quali"]["cond"]["src"]["road"] == "Eingabe"
    rv = {v["key"]: v for v in ev2["race"]["plan"]["variants"]}
    qv = {v["key"]: v for v in ev2["quali"]["plan"]["variants"]}
    assert qv["quali_kurz"]["fuel"] < qv["quali"]["fuel"] < rv["race"]["fuel"]
    names = pipeline.variant_names(ev2, SETUP)
    assert names["race"] == f"20C_27C_{rv['race']['fuel']}F.json" and names["quali"].startswith("21C_31C_")
    assert len(set(names.values())) == 3
    saved = pipeline.save_setups(setup, ev2, tmp_path, SETUP)
    assert set(saved) == {"race", "quali", "quali_kurz"}
    assert setupio.load(str(saved["quali_kurz"]))["basicSetup"]["strategy"]["fuel"] == 20


def test_live_log_attach(loaded, tmp_path):
    """Live-Mitschnitt (ACC Shared Memory) wird über die Uhrzeit den Sessions zugeordnet und hat Vorrang."""
    import json
    from accanalyzer import livelog, pipeline
    df, meta = loaded
    meta = dict(meta, sessions=[dict(s) for s in meta["sessions"]])
    rec = meta["rec_start"]
    rows = []
    for s in meta["sessions"]:
        typ = 2 if s["type"] == "Rennen" else 1
        length = 1200 if typ == 2 else 900
        for k in range(0, int(s["end"] - s["start"]), 5):
            rows.append(dict(ts=rec + s["start"] + k, session=typ, left=max(0, length - 30 - k), air=18.4, road=29.6,
                             car="mclaren_720s_gt3_evo", track="Silverstone", fuel=40 - k / 125 * 3.0, pit=0))
    from datetime import datetime
    f = tmp_path / f"live_{datetime.fromtimestamp(rows[0]['ts']):%Y-%m-%d}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    assert livelog.attach(meta, tmp_path) == 6
    assert meta["sessions"][0]["type"] == "Quali" and meta["sessions"][0]["live"]["minutes"] == 15
    c = pipeline.default_conditions(meta, None, SETUP)
    assert c["race"]["air"] == 18 and c["race"]["road"] == 30 and c["race"]["src"]["road"] == "live"
    assert c["race"]["minutes"] == 20
    assert abs(c["race"]["fuel_per_lap"] - 3.0) < 0.3                       # gemessener Verbrauch


def test_all_sessions(loaded):
    df, meta = loaded
    res = analysis.analyze(df, meta, None)
    assert res.stats["selected"] == list(range(6))
    assert res.stats["laps_valid"] == 33


def test_save_keeps_acc_format(tmp_path):
    """Gespeichertes Setup = Original-Bytes bis auf die geänderten Werte (Tabs, CRLF, 17-stellige Floats)."""
    raw = SETUP.read_bytes()
    setup = setupio.load(str(SETUP))
    assert setupio.dumps(setup).encode("utf-8") == raw
    setup["basicSetup"]["tyres"]["tyrePressure"][2] -= 1
    out = tmp_path / "x.json"
    setupio.save(setup, str(out))
    a, b = raw.decode().split("\r\n"), out.read_bytes().decode().split("\r\n")
    assert len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1
