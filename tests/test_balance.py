"""Echtzeit Unter-/Übersteuern mit Ton (livebalance)."""
import math
from types import SimpleNamespace as NS

from accanalyzer.livebalance import GAP_FAST, GAP_SLOW, BalanceMonitor


def frame(sa_front, sa_rear, speed=150, glat=1.5, brake=0.0, gas=0.0, steer=0.1, yaw=0.5):
    p = NS(speedKmh=speed, accG=[glat, 0, 0], slipAngle=[math.radians(x) for x in (sa_front, sa_front, sa_rear, sa_rear)],
           steerAngle=steer, localAngularVel=[0, yaw, 0], brake=brake, gas=gas)
    return p, NS(isInPit=0, isInPitLane=0)


def drive(mon, frames, t0=0.0, hz=25):
    t = t0
    for p, g in frames:
        mon.update(p, g, t)
        t += 1 / hz
    return t


def test_understeer_oversteer_counts_and_gap():
    mon = BalanceMonitor(dict(us_deg=1.5, os_deg=0.75, slow_kmh=100))
    t = drive(mon, [frame(4, 4)] * 60)                          # neutral: lernt Einheit (Radiant) + Lenkrichtung
    assert mon.kind is None and mon._rad is True
    t = drive(mon, [frame(6, 3.5, speed=80, gas=0.0)] * 10, t)  # langsam, Rollen, 2,5° mehr vorne
    assert mon.active(t)[0] == "us"
    assert mon.counts[("slow", "us")]["coast"] == 1
    t = drive(mon, [frame(4, 4)] * 10, t)
    t = drive(mon, [frame(3, 6, speed=180, gas=0.8)] * 10, t)   # schnell, Gas, Heck rutscht
    assert mon.active(t)[0] == "os" and mon.counts[("fast", "os")]["accel"] == 1
    assert mon.counts[("slow", "us")]["coast"] == 1             # eine Episode zählt nur einmal
    weak, strong = mon.gap(0.1), mon.gap(1.0)
    assert GAP_FAST <= strong < weak <= GAP_SLOW                 # stärker -> schnellere Tonfolge


def test_no_alarm_on_straight_or_pit():
    mon = BalanceMonitor()
    drive(mon, [frame(4, 4)] * 60)
    drive(mon, [frame(8, 2, glat=0.2)] * 20)                     # geradeaus
    assert mon.kind is None
    p, g = frame(8, 2)
    g.isInPit = 1
    mon.update(p, g, 100)
    assert mon.kind is None


def test_sound_loop_beeps_faster_when_stronger():
    import time
    beeps = []
    mon = BalanceMonitor(beep=lambda f, ms: beeps.append((time.time(), f)))
    drive(mon, [frame(4, 4)] * 60)
    mon.start_sound()
    try:
        end = time.time() + 1.2
        while time.time() < end:                                 # starkes Untersteuern (5° mehr vorne)
            mon.update(*frame(9, 4), time.time())
            time.sleep(0.04)
    finally:
        mon.stop()
    assert len(beeps) >= 5 and all(f == mon.s["tone_us"] for _, f in beeps)
