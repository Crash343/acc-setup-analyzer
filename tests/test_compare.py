"""Vergleich mit einer Referenzrunde (compare)."""
from pathlib import Path

import pytest

from accanalyzer import compare, setupio
from accanalyzer.ldimport import load_ld

EX = Path(__file__).resolve().parent.parent / "exmaple_files_for_testings"
LD = EX / "Silverstone-mclaren_720s_gt3_evo-0-2026.11.10-21.46.29.ld"
SETUP = EX / "20C_24C_40F.json"
pytestmark = pytest.mark.skipif(not LD.exists(), reason="Beispielaufnahme fehlt")


@pytest.fixture(scope="module")
def loaded():
    return load_ld(LD)


def test_same_lap_has_no_difference(loaded):
    df, meta = loaded
    c = compare.compare(df, meta, df, meta)
    assert c["total"] == 0 and abs(c["trace"].delta).max() < 1e-9
    assert all(abs(s["dt"]) < 1e-9 and not s["why"] for s in c["sections"])


def test_sections_add_up_and_explain(loaded):
    df, meta = loaded
    v = compare.valid_laps(meta)
    c = compare.compare(df, meta, df, meta, own_lap=v[3], ref_lap=v[0])
    assert len(c["sections"]) >= 12                                        # Silverstone: ~15 Abschnitte
    assert abs(sum(s["dt"] for s in c["sections"]) - c["total"]) < 0.01    # Abschnitte ergeben die Rundendifferenz
    assert c["summary"][0].startswith("Rundenzeit +0.850 s")
    brakes = [s["brake_r"] for s in c["sections"] if s["brake_r"] is not None]
    assert brakes == sorted(brakes) and len(set(brakes)) == len(brakes)    # jeder Bremspunkt gehört zu genau einer Kurve
    lost = max(c["sections"], key=lambda s: s["dt"])
    assert lost["why"]


def test_setup_diff():
    a = setupio.load(str(SETUP))
    b = setupio.load(str(SETUP))
    b["basicSetup"]["tyres"]["tyrePressure"][0] += 3
    b["basicSetup"]["strategy"]["fuel"] = 99
    d = compare.setup_diff(a, b)
    assert len(d) == 1 and d[0][0] == "basicSetup.tyres.tyrePressure[0]" and d[0][2] - d[0][1] == 3


def test_track_map_closes_and_has_lap_length(loaded):
    import numpy as np
    df, meta = loaded
    c = compare.compare(df, meta, df, meta)
    x, y = c["map"]
    assert len(x) == compare.N and np.hypot(x[-1] - x[0], y[-1] - y[0]) < 1.0      # Runde geschlossen
    length = np.hypot(np.diff(x), np.diff(y)).sum()
    assert abs(length - c["length"]) / c["length"] < 0.03                          # Kartenlänge ≈ Rundenlänge
    assert 1 <= len(c["worst"]) <= 3 or c["total"] == 0


REF = EX / "Silverstone-mclaren_720s_gt3_evo-3-2023.08.04-14.37.57.ld"


@pytest.mark.skipif(not REF.exists(), reason="Referenz-Hotlap fehlt")
def test_single_lap_export_without_beacons():
    df, meta = load_ld(REF)
    assert [(l["kind"], round(l["dur"], 1), l["valid"]) for l in meta["laps_info"]] == [("gezeitet", 116.9, True)]
