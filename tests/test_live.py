"""Live-Auswertung (ohne MoTeC-File): die Beispielaufnahme wird als Shared-Memory-Strom nachgespielt.

Ausführen:  python -m pytest tests   (wird übersprungen, wenn die Beispieldatei fehlt)
"""
from pathlib import Path

import numpy as np
import pytest

from accanalyzer import analysis, livesession, pipeline, setupio
from accanalyzer.ldimport import load_ld
from accanalyzer.schema import WHEELS

EX = Path(__file__).resolve().parent.parent / "exmaple_files_for_testings"
LD = EX / "Silverstone-mclaren_720s_gt3_evo-0-2026.11.10-21.46.29.ld"
SETUP = EX / "20C_24C_40F.json"

pytestmark = pytest.mark.skipif(not LD.exists(), reason="Beispielaufnahme fehlt")


@pytest.fixture(scope="module")
def loaded():
    return load_ld(LD)


def replay(df, meta, sid: int, typ: int, hz: float = 25.0, slip=None) -> np.ndarray:
    """Session `sid` der Aufnahme als Rohzeilen, wie sie der Recorder aus dem Shared Memory schreiben würde."""
    s = meta["sessions"][sid]
    laps = [l for l in meta["laps_info"] if l["session"] == sid]
    t = np.arange(s["start"], laps[-1]["end"], 1.0 / hz)
    idx = np.clip(np.searchsorted(df.t.values, t), 0, len(df) - 1)
    d = df.iloc[idx].reset_index(drop=True)
    starts = np.array([l["start"] for l in laps])
    k = np.searchsorted(starts, t, side="right") - 1          # Runde je Sample
    completed = k + 5                                           # ACC zählt ab Session-Beginn – Offset egal
    last_ms = np.array([laps[i - 1]["dur"] * 1000 if i > 0 else 0 for i in k])
    raw = np.full((len(t), len(livesession.RAW)), np.nan)
    C = livesession.COL
    raw[:, C["t"]] = meta["rec_start"] + t
    raw[:, C["laps"]] = completed
    raw[:, C["cur_ms"]] = (t - starts[k]) * 1000
    raw[:, C["last_ms"]] = last_ms
    raw[:, C["npos"]] = d.pos.values
    raw[:, C["pit"]] = 0
    for c in ("speed", "gas", "brake", "steer", "rpm", "g_lat", "g_lon", "yaw"):
        raw[:, C[c]] = d[c].values
    raw[:, C["gear"]] = d.gear.values + 1
    raw[:, C["tc_act"]], raw[:, C["abs_act"]] = 0, 0
    for w in WHEELS:
        for pre in ("w", "p", "tc", "bt", "st"):
            raw[:, C[f"{pre}_{w}"]] = d[f"{pre}_{w}"].values
        if slip:
            raw[:, C[f"sa_{w}"]] = np.radians(slip[w]) * np.sign(d.g_lat.values)   # ACC: Radiant, Vorzeichen je Kurve
    raw[:, C["fuel"]] = 60 - np.arange(len(t)) / hz * 0.025
    raw[:, C["bb"]], raw[:, C["air"]], raw[:, C["road"]] = 0.56, 19.6, 27.4
    raw[:, C["left"]] = np.maximum(0, 1200 - t + s["start"] - 20)
    return raw


def test_live_build_matches_motec(loaded):
    df, meta = loaded
    race = next(s["id"] for s in meta["sessions"] if s["type"] == "Rennen")
    raw = replay(df, meta, race, typ=2)
    ldf, lmeta = livesession.build(raw, dict(session=2, car="mclaren_720s_gt3_evo", track="Silverstone"))
    s = lmeta["sessions"][0]
    assert s["type"] == "Rennen" and s["live"]["minutes"] == 20 and round(s["live"]["road"]) == 27
    motec = [l for l in meta["laps_info"] if l["session"] == race]
    # letzte Runde der Session läuft live noch -> nicht dabei; sonst dieselben gültigen Runden
    assert s["laps_valid"] >= sum(l["valid"] for l in motec[:-1]) - 1
    assert lmeta["laps_info"][0]["kind"] == "Start"
    assert livesession.default_mode(lmeta) == "race"
    res_live = analysis.analyze(ldf, lmeta, None, "race")
    res_motec = analysis.analyze(df, meta, [race], "race")
    for w in WHEELS:   # gleiche Runden, gleiche Daten -> praktisch gleicher Ø-Druck
        assert abs(res_live.stats["pressure"]["avg"][w] - res_motec.stats["pressure"]["avg"][w]) < 0.1
    assert res_live.stats["corners"]


def test_live_slip_angle_understeer(loaded):
    """Schräglauf vorne deutlich größer als hinten -> Untersteuern je Kurve und als Empfehlung."""
    df, meta = loaded
    race = next(s["id"] for s in meta["sessions"] if s["type"] == "Rennen")
    raw = replay(df, meta, race, typ=0, slip=dict(fl=6.0, fr=6.0, rl=3.0, rr=3.0))
    ldf, lmeta = livesession.build(raw, dict(session=0, car="mclaren_720s_gt3_evo", track="Silverstone"))
    assert lmeta["sessions"][0]["type"] == "Training"
    assert abs(ldf.sa_fl.abs().max() - 6.0) < 0.01                     # Radiant -> Grad
    res = analysis.analyze(ldf, lmeta, None, "race")
    mids = [c["phases"]["mid"]["verdict"] for c in res.stats["corners"] if "mid" in c["phases"]]
    assert mids and all(v == "us" for v in mids)
    titles = [f.title for f in res.findings if f.area == "Balance" and f.level == "action"]
    assert any("Untersteuern" in t for t in titles)


def test_live_evaluate_with_setup(loaded, tmp_path):
    """Live-Daten -> Empfehlungen + Setup-Datei mit Live-Bedingungen im Namen (wie nach dem MoTeC-Import)."""
    df, meta = loaded
    race = next(s["id"] for s in meta["sessions"] if s["type"] == "Rennen")
    ldf, lmeta = livesession.build(replay(df, meta, race, typ=2), dict(session=2, car="mclaren_720s_gt3_evo", track="Silverstone"))
    setup = setupio.load(str(SETUP))
    ev = pipeline.evaluate(ldf, lmeta, {"race": [0]}, setup, None, SETUP)
    c = ev["race"]["cond"]
    assert c["air"] == 20 and c["road"] == 27 and c["minutes"] == 20 and c["src"]["air"] == "live"
    names = pipeline.variant_names(ev, SETUP)
    assert names["race"].startswith("20C_27C_")
    saved = pipeline.save_setups(setup, ev, tmp_path, SETUP)
    assert saved["race"].exists()
    md, html, _ = pipeline.write_report(ldf, lmeta, ev, tmp_path, "live")
    assert "Balance je Kurve" in md and html.exists()


def test_recorder_restart():
    """Neue Session (Runden-Zähler springt zurück) -> Puffer beginnt neu."""
    from types import SimpleNamespace as NS
    rec = livesession.Recorder()
    F4 = [0.0] * 4

    def frame(laps, left, session=0):
        p = NS(speedKmh=100, gas=1, brake=0, steerAngle=0, gear=4, rpms=7000, accG=[0, 0, 0], localAngularVel=[0, 0, 0],
               tcinAction=0, absInAction=0, wheelAngularSpeed=F4, wheelsPressure=F4, tyreCoreTemperature=F4, brakeTemp=F4,
               suspensionTravel=F4, wheelSlip=F4, slipAngle=F4, fuel=50, brakeBias=0.56, airTemp=20, roadTemp=28)
        g = NS(completedLaps=laps, sessionTimeLeft=left * 1000, session=session, iCurrentTime=0, iLastTime=0,
               normalizedCarPosition=0.5, isInPit=0, isInPitLane=0, tyreCompound=[0] * 33)
        return p, g, None
    for i in range(10):
        rec.add(*frame(3 + i // 5, 900 - i), now=1000 + i, car="c", track="t")
    assert rec.samples == 10 and rec.laps_done == 1
    rec.add(*frame(0, 900), now=1011, car="c", track="t")
    assert rec.samples == 1 and rec.laps_done == 0
    rec.add(*frame(0, 899, session=2), now=1012, car="c", track="t")   # andere Session-Art
    assert rec.samples == 1 and rec.info["session"] == 2
